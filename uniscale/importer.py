import csv
import io
import json
import logging
import os
import re
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from uniscale.models import (
    ActivityWorkout,
    BloodPressureMeasurement,
    BloodTestMeasurement,
    DailyActivity,
    EcgRecording,
    LabMarker,
    SleepSession,
    WeightMeasurement,
    WorkoutSplit,
)
from uniscale.config import vault_db_path

logger = logging.getLogger(__name__)


def _semicircles_to_degrees(sc: Optional[int]) -> Optional[float]:
    """Converts Garmin/FIT semicircles (32-bit integer) to GPS decimal degrees."""
    if sc is None:
        return None
    # 0x7FFFFFFF (2147483647) or -0x80000000 indicates an invalid / unassigned coordinate in FIT
    if sc == 2147483647 or sc == -2147483648:
        return None
    return round(sc * (180.0 / 2147483648.0), 6)


def parse_garmin_fit_file(fit_path: str) -> Optional[ActivityWorkout]:
    """
    Parses a Garmin .FIT activity file using fitdecode.
    Extracts sport, duration, distance, calories, avg/max heart rate, elevation gain/loss,
    running dynamics (stride length, ground contact time, balance, vertical oscillation,
    vertical ratio, cadence, power), splits (from laps), and GPS track points.
    """
    try:
        import fitdecode

        sport = "generic"
        sub_sport = None
        duration = 0.0
        distance = 0.0
        calories = None
        avg_hr = None
        max_hr = None
        avg_cadence = None
        max_cadence = None
        elevation_gain = None
        elevation_loss = None
        avg_power = None
        max_power = None
        normalized_power = None
        aerobic_te = None
        anaerobic_te = None
        training_load = None
        stride_length_m = None
        ground_contact_ms = None
        ground_contact_balance = None
        vertical_oscillation_mm = None
        vertical_ratio_percent = None
        start_lat = None
        start_lon = None
        end_lat = None
        end_lon = None
        ts = None
        has_session = False

        splits: List[WorkoutSplit] = []
        gps_track: List[Tuple[float, float]] = []
        hr_samples_raw: List[Tuple[int, int]] = []

        # Running dynamics accumulators (fallback if summary missing from session)
        rec_step_lengths: List[float] = []
        rec_stance_times: List[float] = []
        rec_stance_balances: List[float] = []
        rec_vert_oscillations: List[float] = []
        rec_vert_ratios: List[float] = []
        rec_powers: List[float] = []
        rec_cadences: List[float] = []

        with fitdecode.FitReader(fit_path) as fit:
            for frame in fit:
                if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                    continue

                if frame.name == "session":
                    has_session = True
                    if frame.has_field("sport") and frame.get_value("sport") is not None:
                        sport = str(frame.get_value("sport")).lower()
                    if frame.has_field("sub_sport") and frame.get_value("sub_sport") is not None:
                        sub_sport = str(frame.get_value("sub_sport")).lower()
                    if frame.has_field("total_timer_time") and frame.get_value("total_timer_time") is not None:
                        duration = float(frame.get_value("total_timer_time"))
                    elif frame.has_field("total_elapsed_time") and frame.get_value("total_elapsed_time") is not None:
                        duration = float(frame.get_value("total_elapsed_time"))
                    if frame.has_field("total_distance") and frame.get_value("total_distance") is not None:
                        distance = float(frame.get_value("total_distance"))
                    if frame.has_field("total_calories") and frame.get_value("total_calories") is not None:
                        calories = int(frame.get_value("total_calories"))
                    if frame.has_field("avg_heart_rate") and frame.get_value("avg_heart_rate") is not None:
                        avg_hr = int(frame.get_value("avg_heart_rate"))
                    if frame.has_field("max_heart_rate") and frame.get_value("max_heart_rate") is not None:
                        max_hr = int(frame.get_value("max_heart_rate"))
                    if frame.has_field("avg_cadence") and frame.get_value("avg_cadence") is not None:
                        avg_cadence = int(frame.get_value("avg_cadence"))
                    if frame.has_field("max_cadence") and frame.get_value("max_cadence") is not None:
                        max_cadence = int(frame.get_value("max_cadence"))
                    if frame.has_field("total_ascent") and frame.get_value("total_ascent") is not None:
                        elevation_gain = float(frame.get_value("total_ascent"))
                    if frame.has_field("total_descent") and frame.get_value("total_descent") is not None:
                        elevation_loss = float(frame.get_value("total_descent"))
                    if frame.has_field("avg_power") and frame.get_value("avg_power") is not None:
                        avg_power = int(frame.get_value("avg_power"))
                    if frame.has_field("max_power") and frame.get_value("max_power") is not None:
                        max_power = int(frame.get_value("max_power"))
                    if frame.has_field("normalized_power") and frame.get_value("normalized_power") is not None:
                        normalized_power = int(frame.get_value("normalized_power"))
                    if frame.has_field("total_training_effect") and frame.get_value("total_training_effect") is not None:
                        aerobic_te = round(float(frame.get_value("total_training_effect")), 1)
                    if frame.has_field("total_anaerobic_training_effect") and frame.get_value("total_anaerobic_training_effect") is not None:
                        anaerobic_te = round(float(frame.get_value("total_anaerobic_training_effect")), 1)
                    if frame.has_field("training_load_peak") and frame.get_value("training_load_peak") is not None:
                        training_load = round(float(frame.get_value("training_load_peak")), 1)

                    # Running dynamics from session summary
                    if frame.has_field("avg_step_length") and frame.get_value("avg_step_length") is not None:
                        sl = float(frame.get_value("avg_step_length"))
                        stride_length_m = round(sl / 1000.0, 2) if sl > 10 else round(sl, 2)
                    if frame.has_field("avg_stance_time") and frame.get_value("avg_stance_time") is not None:
                        ground_contact_ms = round(float(frame.get_value("avg_stance_time")), 1)
                    if frame.has_field("avg_stance_time_balance") and frame.get_value("avg_stance_time_balance") is not None:
                        ground_contact_balance = round(float(frame.get_value("avg_stance_time_balance")), 2)
                    if frame.has_field("avg_vertical_oscillation") and frame.get_value("avg_vertical_oscillation") is not None:
                        vertical_oscillation_mm = round(float(frame.get_value("avg_vertical_oscillation")), 1)
                    if frame.has_field("avg_vertical_ratio") and frame.get_value("avg_vertical_ratio") is not None:
                        vertical_ratio_percent = round(float(frame.get_value("avg_vertical_ratio")), 2)

                    # Coordinates from session
                    if frame.has_field("start_position_lat") and frame.has_field("start_position_long"):
                        start_lat = _semicircles_to_degrees(frame.get_value("start_position_lat"))
                        start_lon = _semicircles_to_degrees(frame.get_value("start_position_long"))
                    if frame.has_field("end_position_lat") and frame.has_field("end_position_long"):
                        end_lat = _semicircles_to_degrees(frame.get_value("end_position_lat"))
                        end_lon = _semicircles_to_degrees(frame.get_value("end_position_long"))

                    if frame.has_field("start_time") and frame.get_value("start_time") is not None:
                        ts = frame.get_value("start_time")

                elif frame.name == "lap":
                    # Laps / splits
                    lap_dist = float(frame.get_value("total_distance")) if frame.has_field("total_distance") and frame.get_value("total_distance") is not None else 0.0
                    lap_time = float(frame.get_value("total_timer_time")) if frame.has_field("total_timer_time") and frame.get_value("total_timer_time") is not None else (
                        float(frame.get_value("total_elapsed_time")) if frame.has_field("total_elapsed_time") and frame.get_value("total_elapsed_time") is not None else 0.0
                    )
                    lap_hr = int(frame.get_value("avg_heart_rate")) if frame.has_field("avg_heart_rate") and frame.get_value("avg_heart_rate") is not None else None
                    if lap_dist > 0 or lap_time > 0:
                        splits.append(WorkoutSplit(distance_m=round(lap_dist, 1), seconds=round(lap_time, 1), avg_hr=lap_hr))

                elif frame.name == "record":
                    # Track coordinates
                    if frame.has_field("position_lat") and frame.has_field("position_long"):
                        lat_val = frame.get_value("position_lat")
                        lon_val = frame.get_value("position_long")
                        lat_deg = _semicircles_to_degrees(lat_val)
                        lon_deg = _semicircles_to_degrees(lon_val)
                        if lat_deg is not None and lon_deg is not None:
                            gps_track.append((lat_deg, lon_deg))

                    # Heart rate series
                    rec_ts = frame.get_value("timestamp") if frame.has_field("timestamp") else None
                    if frame.has_field("heart_rate") and frame.get_value("heart_rate") is not None:
                        bpm = int(frame.get_value("heart_rate"))
                        if ts is not None and rec_ts is not None and hasattr(rec_ts, "timestamp"):
                            sec = max(0, int((rec_ts - ts).total_seconds()))
                            hr_samples_raw.append((sec, bpm))

                    # Record dynamics accumulators (fallback)
                    if frame.has_field("step_length") and frame.get_value("step_length") is not None:
                        rec_step_lengths.append(float(frame.get_value("step_length")))
                    if frame.has_field("stance_time") and frame.get_value("stance_time") is not None:
                        rec_stance_times.append(float(frame.get_value("stance_time")))
                    if frame.has_field("stance_time_balance") and frame.get_value("stance_time_balance") is not None:
                        rec_stance_balances.append(float(frame.get_value("stance_time_balance")))
                    if frame.has_field("vertical_oscillation") and frame.get_value("vertical_oscillation") is not None:
                        rec_vert_oscillations.append(float(frame.get_value("vertical_oscillation")))
                    if frame.has_field("vertical_ratio") and frame.get_value("vertical_ratio") is not None:
                        rec_vert_ratios.append(float(frame.get_value("vertical_ratio")))
                    if frame.has_field("power") and frame.get_value("power") is not None:
                        rec_powers.append(float(frame.get_value("power")))
                    if frame.has_field("cadence") and frame.get_value("cadence") is not None:
                        rec_cadences.append(float(frame.get_value("cadence")))

                elif frame.name == "activity":
                    if ts is None and frame.has_field("timestamp") and frame.get_value("timestamp") is not None:
                        ts = frame.get_value("timestamp")

        if not has_session:
            return None  # not a workout (sleep, HRV, metrics, monitoring...)
        if ts is None:
            ts = datetime.fromtimestamp(os.path.getmtime(fit_path), tz=timezone.utc)
        elif not hasattr(ts, "tzinfo") or ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

        # Fallback calculations from records if session lacked dynamics
        if stride_length_m is None and rec_step_lengths:
            avg_sl = sum(rec_step_lengths) / len(rec_step_lengths)
            stride_length_m = round(avg_sl / 1000.0, 2) if avg_sl > 10 else round(avg_sl, 2)
        if ground_contact_ms is None and rec_stance_times:
            ground_contact_ms = round(sum(rec_stance_times) / len(rec_stance_times), 1)
        if ground_contact_balance is None and rec_stance_balances:
            ground_contact_balance = round(sum(rec_stance_balances) / len(rec_stance_balances), 2)
        if vertical_oscillation_mm is None and rec_vert_oscillations:
            vertical_oscillation_mm = round(sum(rec_vert_oscillations) / len(rec_vert_oscillations), 1)
        if vertical_ratio_percent is None and rec_vert_ratios:
            vertical_ratio_percent = round(sum(rec_vert_ratios) / len(rec_vert_ratios), 2)
        if avg_power is None and rec_powers:
            avg_power = round(sum(rec_powers) / len(rec_powers))
        if avg_cadence is None and rec_cadences:
            avg_cadence = round(sum(rec_cadences) / len(rec_cadences))

        # Start and end coordinates fallback from track
        if start_lat is None and gps_track:
            start_lat, start_lon = gps_track[0]
        if end_lat is None and gps_track:
            end_lat, end_lon = gps_track[-1]

        # Downsample HR samples if huge (keep at most ~500 points for responsiveness)
        hr_samples: Optional[List[Tuple[int, int]]] = None
        if hr_samples_raw:
            if len(hr_samples_raw) > 500:
                step = max(1, len(hr_samples_raw) // 250)
                hr_samples = hr_samples_raw[::step]
                if hr_samples[-1] != hr_samples_raw[-1]:
                    hr_samples.append(hr_samples_raw[-1])
            else:
                hr_samples = hr_samples_raw

        # Downsample GPS track if huge (> 4000 points)
        final_track: Optional[List[Tuple[float, float]]] = None
        if gps_track:
            if len(gps_track) > 4000:
                step = max(1, len(gps_track) // 2000)
                final_track = gps_track[::step]
                if final_track[-1] != gps_track[-1]:
                    final_track.append(gps_track[-1])
            else:
                final_track = gps_track

        return ActivityWorkout(
            sport=sport,
            sub_sport=sub_sport,
            timestamp=ts,
            duration_seconds=round(duration, 1),
            distance_meters=round(distance, 1),
            calories=calories,
            avg_heart_rate=avg_hr,
            max_heart_rate=max_hr,
            avg_cadence=avg_cadence,
            max_cadence=max_cadence,
            elevation_gain_m=elevation_gain,
            elevation_loss_m=elevation_loss,
            avg_power=avg_power,
            max_power=max_power,
            normalized_power=normalized_power,
            aerobic_training_effect=aerobic_te,
            anaerobic_training_effect=anaerobic_te,
            training_load=training_load,
            stride_length_m=stride_length_m,
            ground_contact_ms=ground_contact_ms,
            ground_contact_balance=ground_contact_balance,
            vertical_oscillation_mm=vertical_oscillation_mm,
            vertical_ratio_percent=vertical_ratio_percent,
            start_lat=start_lat,
            start_lon=start_lon,
            end_lat=end_lat,
            end_lon=end_lon,
            gps_track=final_track,
            splits=splits if splits else None,
            hr_samples=hr_samples,
            device_name="Garmin",
            source_file=os.path.basename(fit_path)
        )
    except Exception as e:
        logger.error(f"Failed to parse FIT file {fit_path}: {e}")
        return None


def parse_gpx_file(gpx_path: str) -> Optional[ActivityWorkout]:
    """
    Parses a standard GPX file (e.g. exported from Gadgetbridge, Strava, or GPS devices).
    Extracts track coordinates, total distance, duration, elevation gain/loss,
    heart rate samples, and cadence.
    """
    try:
        from uniscale.places import haversine_distance

        tree = ET.parse(gpx_path)
        root = tree.getroot()

        # Handle XML namespaces
        ns = {"gpx": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
        find_tag = lambda el, tag: el.find(f"gpx:{tag}", ns) if ns else el.find(tag)
        findall_tags = lambda el, tag: el.findall(f".//gpx:{tag}", ns) if ns else el.findall(f".//{tag}")

        sport = "running"
        type_elem = root.find(".//gpx:type", ns) if ns else root.find(".//type")
        if type_elem is not None and type_elem.text:
            sport = type_elem.text.strip().lower()

        trkpts = findall_tags(root, "trkpt")
        if not trkpts:
            return None

        points: List[Tuple[float, float]] = []
        elevations: List[float] = []
        timestamps: List[datetime] = []
        hr_samples_raw: List[Tuple[int, int]] = []
        cadences: List[int] = []

        for pt in trkpts:
            lat = float(pt.attrib["lat"])
            lon = float(pt.attrib["lon"])
            points.append((lat, lon))

            ele_elem = find_tag(pt, "ele")
            if ele_elem is not None and ele_elem.text:
                try:
                    elevations.append(float(ele_elem.text))
                except ValueError:
                    pass

            time_elem = find_tag(pt, "time")
            if time_elem is not None and time_elem.text:
                try:
                    t_str = time_elem.text.strip().replace("Z", "+00:00")
                    timestamps.append(datetime.fromisoformat(t_str))
                except Exception:
                    pass

            # Extensions (heart rate, cadence)
            for child in pt.iter():
                tag_lower = child.tag.lower()
                if tag_lower.endswith("hr") and child.text:
                    try:
                        hr_val = int(child.text.strip())
                        if timestamps:
                            t_sec = max(0, int((timestamps[-1] - timestamps[0]).total_seconds()))
                            hr_samples_raw.append((t_sec, hr_val))
                    except ValueError:
                        pass
                elif tag_lower.endswith("cad") and child.text:
                    try:
                        cadences.append(int(child.text.strip()))
                    except ValueError:
                        pass

        # Total distance
        total_dist_m = 0.0
        for i in range(1, len(points)):
            total_dist_m += haversine_distance(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1])

        # Duration & Start timestamp
        start_ts = timestamps[0] if timestamps else datetime.fromtimestamp(os.path.getmtime(gpx_path), tz=timezone.utc)
        if start_ts.tzinfo is None:
            start_ts = start_ts.replace(tzinfo=timezone.utc)
        if len(timestamps) >= 2:
            duration_s = max(0.0, (timestamps[-1] - timestamps[0]).total_seconds())
        else:
            duration_s = 0.0

        # Elevation gain / loss
        elev_gain = None
        elev_loss = None
        if len(elevations) >= 2:
            gain = 0.0
            loss = 0.0
            for i in range(1, len(elevations)):
                diff = elevations[i] - elevations[i - 1]
                if diff > 0:
                    gain += diff
                else:
                    loss += abs(diff)
            elev_gain = round(gain, 1)
            elev_loss = round(loss, 1)

        # Heart rate stats
        avg_hr = None
        max_hr = None
        if hr_samples_raw:
            bpms = [s[1] for s in hr_samples_raw]
            avg_hr = round(sum(bpms) / len(bpms))
            max_hr = max(bpms)

        avg_cad = round(sum(cadences) / len(cadences)) if cadences else None

        # Downsample HR samples if huge
        hr_samples = None
        if hr_samples_raw:
            if len(hr_samples_raw) > 500:
                step = max(1, len(hr_samples_raw) // 250)
                hr_samples = hr_samples_raw[::step]
                if hr_samples[-1] != hr_samples_raw[-1]:
                    hr_samples.append(hr_samples_raw[-1])
            else:
                hr_samples = hr_samples_raw

        # Downsample track if huge
        final_track = points
        if len(points) > 4000:
            step = max(1, len(points) // 2000)
            final_track = points[::step]
            if final_track[-1] != points[-1]:
                final_track.append(points[-1])

        return ActivityWorkout(
            sport=sport,
            timestamp=start_ts,
            duration_seconds=round(duration_s, 1),
            distance_meters=round(total_dist_m, 1),
            avg_heart_rate=avg_hr,
            max_heart_rate=max_hr,
            avg_cadence=avg_cad,
            elevation_gain_m=elev_gain,
            elevation_loss_m=elev_loss,
            start_lat=points[0][0],
            start_lon=points[0][1],
            end_lat=points[-1][0],
            end_lon=points[-1][1],
            gps_track=final_track,
            hr_samples=hr_samples,
            device_name="GPX Export",
            source_file=os.path.basename(gpx_path)
        )
    except Exception as e:
        logger.error(f"Failed to parse GPX file {gpx_path}: {e}")
        return None


SLEEP_LEVELS = {"deep": "deep_sleep_seconds", "light": "light_sleep_seconds",
                "rem": "rem_sleep_seconds", "awake": "awake_seconds"}


def parse_garmin_sleep_fit(sleep_fit_path: str, hrv_fit_path: Optional[str] = None) -> Optional[SleepSession]:
    """
    Parses a Garmin SLEEP FIT file (as downloaded by Gadgetbridge): sleep score, stage
    breakdown and time in bed, plus overnight HRV from the matching HRV_STATUS FIT file.
    The night is dated by its local wake-up date, like Garmin Connect and Health Connect.
    """
    try:
        import fitdecode

        assessment: dict = {}
        levels = []
        start_time = stop_time = None
        with fitdecode.FitReader(sleep_fit_path) as fit:
            for frame in fit:
                if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                    continue
                if frame.name == "sleep_assessment":
                    assessment = {f.name: f.value for f in frame.fields if f.value is not None}
                elif frame.name == "event" and frame.get_value("event", fallback=None) == 74:
                    kind = frame.get_value("event_type", fallback=None)
                    if kind == "start":
                        start_time = frame.get_value("timestamp")
                    elif kind == "stop":
                        stop_time = frame.get_value("timestamp")
                elif frame.name == "sleep_level":
                    levels.append((frame.get_value("timestamp"), frame.get_value("sleep_level")))

        if not levels and not (start_time and stop_time):
            return None
        start_time = start_time or levels[0][0]
        stop_time = stop_time or levels[-1][0]

        stages = dict.fromkeys(SLEEP_LEVELS.values(), 0)
        # Each level lasts until the next one; the last one until the stop event.
        for (t1, lvl), (t2, _) in zip(levels, levels[1:] + [(stop_time, None)]):
            if lvl in SLEEP_LEVELS and t2 > t1:
                stages[SLEEP_LEVELS[lvl]] += int((t2 - t1).total_seconds())
        total = int((stop_time - start_time).total_seconds()) or sum(stages.values())

        hrv_val = hrv_status = None
        if hrv_fit_path and os.path.exists(hrv_fit_path):
            with fitdecode.FitReader(hrv_fit_path) as fit:
                for frame in fit:
                    if frame.frame_type == fitdecode.FIT_FRAME_DATA and frame.name == "hrv_status_summary":
                        hrv_val = frame.get_value("last_night_average", fallback=None)
                        hrv_status = frame.get_value("status", fallback=None)

        # ponytail: server-local time zone; per-user time zones if users span zones
        wake_date = stop_time.astimezone().strftime("%Y-%m-%d")
        return SleepSession(
            timestamp=stop_time,
            date=wake_date,
            duration_seconds=total,
            duration_hours=round(total / 3600.0, 2),
            **stages,
            **{k.replace("_seconds", "_minutes"): round(v / 60) for k, v in stages.items()},
            sleep_score=assessment.get("overall_sleep_score"),
            deep_score=assessment.get("deep_sleep_score"),
            rem_score=assessment.get("rem_sleep_score"),
            quality_score=assessment.get("sleep_quality_score"),
            recovery_score=assessment.get("sleep_recovery_score"),
            avg_sleep_stress=assessment.get("average_stress_during_sleep"),
            hrv_rmssd_ms=float(hrv_val) if hrv_val is not None else None,
            hrv_status=str(hrv_status) if hrv_status is not None else None,
            device_name="Garmin (Gadgetbridge)",
        )
    except Exception as e:
        logger.error(f"Failed to parse sleep FIT file {sleep_fit_path}: {e}")
        return None


def parse_garmin_takeout(path: str, target_db: Optional[str] = None) -> dict:
    """
    Parses a Garmin Connect Takeout archive (.zip) or extracted directory.
    Extracts:
    1. Historical weights & biometrics from userBioMetrics.json
    2. Daily summaries (steps, distance, calories, resting HR) from UDSFile_*.json
    3. Workouts & recorded activities from summarizedActivities.json and .fit files
    """
    weights: List[WeightMeasurement] = []
    daily_summaries: List[DailyActivity] = []
    workouts: List[ActivityWorkout] = []

    def _process_json_content(filename: str, content: str):
        fname_lower = filename.lower()
        try:
            data = json.loads(content)
        except Exception:
            return

        # 1. Weights in userBioMetrics.json
        if "userbiometrics" in fname_lower:
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and "weight" in item and isinstance(item["weight"], dict):
                        wg = item["weight"].get("weight")
                        cal_date = item.get("metaData", {}).get("calendarDate")
                        if wg and cal_date:
                            kg = round(float(wg) / 1000.0, 2)
                            lb = round(kg * 2.20462, 1)
                            try:
                                ts = datetime.fromisoformat(cal_date[:19]).replace(tzinfo=timezone.utc)
                            except Exception:
                                ts = datetime.now(timezone.utc)
                            bmi = float(item["bmi"]) if "bmi" in item and item["bmi"] else None
                            weights.append(
                                WeightMeasurement(
                                    weight_kg=kg,
                                    weight_lb=lb,
                                    bmi=bmi,
                                    timestamp=ts,
                                    device_name="Garmin Connect (Imported)",
                                    device_address="Garmin Connect"
                                )
                            )

        # 2. Daily summaries in UDSFile_*.json
        elif "udsfile_" in fname_lower or ("summaries" in fname_lower and not fname_lower.endswith(".zip")):
            if isinstance(data, list):
                for day in data:
                    if isinstance(day, dict) and "calendarDate" in day:
                        cdate = day["calendarDate"]
                        steps = int(day.get("totalSteps", 0) or 0)
                        dist_m = float(day.get("totalDistanceMeters", 0) or 0)
                        if cdate and (steps > 0 or dist_m > 0):
                            cal = int(day.get("totalKilocalories") or 0) if day.get("totalKilocalories") else None
                            active_sec = day.get("activeSeconds")
                            active_min = int(active_sec / 60) if active_sec else None
                            rhr = int(day.get("restingHeartRate") or 0) if day.get("restingHeartRate") else None
                            daily_summaries.append(
                                DailyActivity(
                                    date=cdate,
                                    steps=steps,
                                    distance_km=round(dist_m / 1000.0, 2),
                                    calories=cal,
                                    active_minutes=active_min,
                                    resting_heart_rate=rhr,
                                    source="Garmin (Imported)"
                                )
                            )

        # 3. Workouts in summarizedActivities.json
        elif "summarizedactivities" in fname_lower:
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict):
                        export_list = item.get("summarizedActivitiesExport", [])
                        for act in export_list:
                            sport = str(act.get("activityType") or act.get("sportType") or "generic").lower()
                            dur_ms = float(act.get("duration") or 0)
                            dist_cm = float(act.get("distance") or 0)
                            dur_s = round(dur_ms / 1000.0, 1)
                            dist_m = round(dist_cm / 100.0, 1)
                            ts_ms = act.get("startTimeGmt") or act.get("beginTimestamp")
                            if ts_ms:
                                try:
                                    ts = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
                                except Exception:
                                    ts = datetime.now(timezone.utc)
                            else:
                                ts = datetime.now(timezone.utc)

                            cal = int(act["calories"]) if "calories" in act and act["calories"] else None
                            avg_hr = int(act["avgHr"]) if "avgHr" in act and act["avgHr"] else None
                            max_hr = int(act["maxHr"]) if "maxHr" in act and act["maxHr"] else None
                            cadence = int(act.get("avgRunCadence") or act.get("avgDoubleCadence") or 0)
                            elev_cm = float(act.get("elevationGain") or 0)

                            workouts.append(
                                ActivityWorkout(
                                    sport=sport,
                                    timestamp=ts,
                                    duration_seconds=dur_s,
                                    distance_meters=dist_m,
                                    distance_km=round(dist_m / 1000.0, 2),
                                    calories=cal,
                                    avg_heart_rate=avg_hr,
                                    max_heart_rate=max_hr,
                                    avg_cadence=cadence if cadence > 0 else None,
                                    elevation_gain_m=round(elev_cm / 100.0, 1) if elev_cm > 0 else None,
                                    device_name="Garmin (Imported)",
                                    source_file=filename
                                )
                            )

    if os.path.isfile(path) and path.lower().endswith(".zip"):
        with zipfile.ZipFile(path, "r") as z:
            for name in z.namelist():
                name_lower = name.lower()
                if name_lower.endswith(".json") and (
                    "userbiometrics" in name_lower or
                    "udsfile_" in name_lower or
                    "summarizedactivities" in name_lower
                ):
                    with z.open(name) as f:
                        content = f.read().decode("utf-8", errors="ignore")
                        _process_json_content(name, content)
                elif name_lower.endswith(".fit"):
                    try:
                        import tempfile
                        with tempfile.NamedTemporaryFile(suffix=".fit", delete=True) as tmp:
                            tmp.write(z.read(name))
                            tmp.flush()
                            w = parse_garmin_fit_file(tmp.name)
                            if w:
                                w.source_file = os.path.basename(name)
                                workouts.append(w)
                    except Exception:
                        pass
                elif name_lower.endswith(".gpx"):
                    try:
                        import tempfile
                        with tempfile.NamedTemporaryFile(suffix=".gpx", delete=True) as tmp:
                            tmp.write(z.read(name))
                            tmp.flush()
                            w = parse_gpx_file(tmp.name)
                            if w:
                                w.source_file = os.path.basename(name)
                                workouts.append(w)
                    except Exception:
                        pass
    elif os.path.isdir(path):
        for root, _, files in os.walk(path):
            for f in files:
                fpath = os.path.join(root, f)
                f_lower = f.lower()
                if f_lower.endswith(".json"):
                    try:
                        with open(fpath, "r", encoding="utf-8", errors="ignore") as jf:
                            _process_json_content(fpath, jf.read())
                    except Exception:
                        pass
                elif f_lower.endswith(".fit"):
                    w = parse_garmin_fit_file(fpath)
                    if w:
                        workouts.append(w)
                elif f_lower.endswith(".gpx"):
                    w = parse_gpx_file(fpath)
                    if w:
                        workouts.append(w)

    save_history(weights, target_path=target_db)
    save_history(daily_summaries, target_path=target_db)
    save_history(workouts, target_path=target_db)

    return {
        "weights_found": len(weights),
        "daily_summaries_found": len(daily_summaries),
        "workouts_found": len(workouts),
    }


def parse_garmin_export_dir(export_dir: str) -> dict:
    return parse_garmin_takeout(export_dir)


GFIT_FIRST_YEAR = "2014"  # Google Fit launched in 2014; earlier dates are device-clock errors
GFIT_SESSION_GAP_S = 2 * 3600  # sleep segments further apart than this belong to different nights
GFIT_WORKOUT_MATCH_S = 1200
# Google Fit sleep stages: 1 awake, 2 asleep (unstaged), 3 out of bed, 4 light, 5 deep, 6 REM
_GFIT_STAGE_FIELD = {1: "awake_seconds", 3: "awake_seconds", 4: "light_sleep_seconds",
                     5: "deep_sleep_seconds", 6: "rem_sleep_seconds"}
_GFIT_SPORTS = {"treadmill running": ("running", "treadmill"), "rowing machine": ("rowing", "indoor_rowing")}
_TCX_NS = {"t": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2"}


class _TakeoutReader:
    """Reads members of a Google Takeout .zip in place (no 6 GB extraction) or of an extracted directory."""

    def __init__(self, path: str):
        self.zip = zipfile.ZipFile(path) if os.path.isfile(path) else None
        if self.zip:
            self.names = self.zip.namelist()
        else:
            self.names = [os.path.relpath(os.path.join(r, f), path).replace(os.sep, "/")
                          for r, _, fs in os.walk(path) for f in fs]
        self.root = path

    def find(self, suffix: str) -> Optional[str]:
        return next((n for n in self.names if n.endswith(suffix)), None)

    def read(self, name: str) -> bytes:
        if self.zip:
            return self.zip.read(name)
        with open(os.path.join(self.root, name), "rb") as f:
            return f.read()


def _gfit_points(reader: _TakeoutReader, file_suffix: str) -> list:
    name = reader.find(file_suffix)
    if not name:
        return []
    return json.loads(reader.read(name)).get("Data Points", [])


def _gfit_values(point: dict) -> list:
    return [v.get("value", {}).get("fpVal", v.get("value", {}).get("intVal")) for v in point.get("fitValue", [])]


def _nanos_to_dt(nanos: Any) -> datetime:
    return datetime.fromtimestamp(int(nanos) / 1e9, tz=timezone.utc)


def _local_date(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d")


def _num(row: dict, col: str) -> Optional[float]:
    try:
        return float(row.get(col) or "")
    except ValueError:
        return None


def _gfit_daily(reader: _TakeoutReader) -> List[DailyActivity]:
    resting = {}
    for p in _gfit_points(reader, "resting_heart_rate_-merge_heart_rate_bpm.json"):
        bpm = _gfit_values(p)[0]
        if bpm:
            resting[_local_date(_nanos_to_dt(p["startTimeNanos"]))] = int(round(bpm))
    name = reader.find("Daily activity metrics/Daily activity metrics.csv")
    if not name:
        return []
    days = []
    for row in csv.DictReader(io.StringIO(reader.read(name).decode("utf-8", errors="ignore"))):
        date = (row.get("Date") or "")[:10]
        if date < GFIT_FIRST_YEAR:
            continue

        def as_int(col):
            v = _num(row, col)
            return int(round(v)) if v else None

        day = DailyActivity(
            date=date,
            steps=as_int("Step count") or 0,
            distance_km=round((_num(row, "Distance (m)") or 0) / 1000.0, 2),
            calories=as_int("Calories (kcal)"),
            active_minutes=as_int("Move Minutes count"),
            resting_heart_rate=resting.get(date),
            avg_heart_rate=as_int("Average heart rate (bpm)"),
            min_heart_rate=as_int("Min heart rate (bpm)"),
            max_heart_rate=as_int("Max heart rate (bpm)"),
            avg_spo2=round(_num(row, "Average oxygen saturation (%)") or 0, 1) or None,
            heart_points=as_int("Heart Points"),
            source="Google Fit (Takeout)",
        )
        if day.steps or day.avg_heart_rate or day.calories:
            days.append(day)
    return days


def _gfit_weights(reader: _TakeoutReader) -> List[WeightMeasurement]:
    out = []
    for p in _gfit_points(reader, "derived_com.google.weight_com.google.android.gms_merge_weight.json"):
        kg = _gfit_values(p)[0]
        if kg:
            out.append(WeightMeasurement(weight_kg=round(kg, 2), timestamp=_nanos_to_dt(p["startTimeNanos"]),
                                         device_name="Google Fit (Imported)", device_address="Google Fit"))
    return out


def _gfit_bps(reader: _TakeoutReader) -> List[BloodPressureMeasurement]:
    out = []
    for p in _gfit_points(reader, "derived_com.google.blood_pressure_com.google.android.gms_merged.json"):
        vals = _gfit_values(p) + [None, None]
        if vals[0] and vals[1]:
            # Google Fit BP points carry no pulse; 0 means "not measured" and is ignored when averaging.
            out.append(BloodPressureMeasurement(systolic=int(vals[0]), diastolic=int(vals[1]), pulse=0,
                                                timestamp=_nanos_to_dt(p["startTimeNanos"]),
                                                device_name="Google Fit (Imported)", device_address="Google Fit"))
    return out


def _gfit_sleep(reader: _TakeoutReader) -> List[SleepSession]:
    segs = sorted(
        ((int(p["startTimeNanos"]) / 1e9, int(p["endTimeNanos"]) / 1e9, _gfit_values(p)[0])
         for p in _gfit_points(reader, "derived_com.google.sleep.segment_com.google.android.gms_merged.json")),
        key=lambda s: s[0])
    nights: List[list] = []
    for seg in segs:
        if nights and seg[0] - nights[-1][-1][1] <= GFIT_SESSION_GAP_S:
            nights[-1].append(seg)
        else:
            nights.append([seg])
    out = []
    for night in nights:
        fields = {f: 0 for f in set(_GFIT_STAGE_FIELD.values())}
        asleep = 0
        for start, end, stage in night:
            secs = int(max(0, end - start))
            if stage in _GFIT_STAGE_FIELD:
                fields[_GFIT_STAGE_FIELD[stage]] += secs
            if stage not in (1, 3):
                asleep += secs
        if not asleep:
            continue
        start = datetime.fromtimestamp(night[0][0], tz=timezone.utc)
        wake = datetime.fromtimestamp(max(s[1] for s in night), tz=timezone.utc)
        out.append(SleepSession(
            timestamp=start, date=_local_date(wake), duration_seconds=asleep,
            duration_hours=round(asleep / 3600, 2), device_name="Google Fit", **fields,
            **{k.replace("_seconds", "_minutes"): v // 60 for k, v in fields.items()}))
    return out


def _gfit_workout(name: str, tcx: bytes) -> Optional[ActivityWorkout]:
    root = ET.fromstring(tcx)
    laps = root.findall(".//t:Lap", _TCX_NS)
    if not laps:
        return None

    def total(tag):
        return sum(float(lap.findtext(f"t:{tag}", "0", _TCX_NS) or 0) for lap in laps)

    secs = total("TotalTimeSeconds")
    hr_avg = [(float(lap.findtext("t:AverageHeartRateBpm/t:Value", "0", _TCX_NS)),
               float(lap.findtext("t:TotalTimeSeconds", "0", _TCX_NS))) for lap in laps]
    hr_max = [float(lap.findtext("t:MaximumHeartRateBpm/t:Value", "0", _TCX_NS)) for lap in laps]
    weighted = sum(h * t for h, t in hr_avg if h)
    hr_secs = sum(t for h, t in hr_avg if h)
    label = os.path.basename(name).rsplit("_", 1)[-1].removesuffix(".tcx").split(",")[0].strip().lower()
    sport, sub_sport = _GFIT_SPORTS.get(label, (label.replace(" ", "_") or "generic", None))
    return ActivityWorkout(
        sport=sport, sub_sport=sub_sport,
        timestamp=datetime.fromisoformat(laps[0].get("StartTime", "").replace("Z", "+00:00")),
        duration_seconds=secs, distance_meters=total("DistanceMeters"),
        calories=int(total("Calories")) or None,
        avg_heart_rate=int(round(weighted / hr_secs)) if hr_secs else None,
        max_heart_rate=int(max(hr_max)) or None,
        device_name="Google Fit", source_file=os.path.basename(name))


def _gfit_workouts(reader: _TakeoutReader) -> List[ActivityWorkout]:
    out = []
    for name in reader.names:
        if "/Fit/Activities/" not in "/" + name or name.endswith("/"):
            continue
        try:
            w = _gfit_workout(name, reader.read(name))
            if w:
                out.append(w)
        except (ET.ParseError, ValueError) as e:
            logger.warning(f"Skipping unreadable Google Fit workout {name}: {e}")
    return out


def _split_already_there(sleeps: List[SleepSession], workouts: List[ActivityWorkout], db: str) -> tuple:
    """
    Sleep and workouts from another source are "already there" when the vault has that night,
    or any workout starting within GFIT_WORKOUT_MATCH_S (sport names differ between sources).
    save_history keeps several records per night on purpose, so this check lives here.
    """
    nights, starts = set(), []
    if os.path.exists(db):
        with open(db, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") == "sleep":
                    nights.add(d.get("date"))
                elif d.get("type") == "workout":
                    ep = _parse_epoch(d.get("timestamp"))
                    if ep:
                        starts.append(ep)
    new_sleeps = [s for s in sleeps if s.date not in nights]
    new_workouts = [w for w in workouts
                    if not any(abs(w.timestamp.timestamp() - s) < GFIT_WORKOUT_MATCH_S for s in starts)]
    dup = ([s.model_dump(mode="json") | {"type": "sleep"} for s in sleeps if s.date in nights]
           + [w.model_dump(mode="json") | {"type": "workout"} for w in workouts if w not in new_workouts])
    return new_sleeps, new_workouts, dup


_DAY_HEART_FIELDS = ("resting_heart_rate", "avg_heart_rate", "min_heart_rate", "max_heart_rate",
                     "avg_spo2", "heart_points")


def _fill_day_gaps(days: List[DailyActivity], db: str) -> int:
    """
    A day the vault already has (from another source) is not imported again, but the heart-rate fields it
    lacks are appended as a fields-only record; reading the vault merges it into that day (steps stay max).
    """
    from uniscale.readings import load_vault
    known = {r.get("date"): r for r in load_vault(db) if r.get("type") == "activity"}
    fills = []
    for day in days:
        have = known.get(day.date)
        if have is None:
            continue
        gaps = {f: getattr(day, f) for f in _DAY_HEART_FIELDS
                if getattr(day, f) is not None and have.get(f) in (None, 0)}
        if gaps:
            fills.append({"type": "activity", "date": day.date, "steps": 0, "distance_km": 0.0,
                          "source": "", **gaps})
    with open(db, "a", encoding="utf-8") as f:
        f.writelines(json.dumps(d) + "\n" for d in fills)
    return len(fills)


def parse_google_fit_takeout(takeout_path: str, target_db: Optional[str] = None) -> dict:
    """
    Imports a Google Fit Takeout export (.zip or extracted directory): daily summaries (steps, calories,
    move minutes, avg/min/max/resting heart rate, SpO2, heart points), every weight and blood-pressure
    reading, sleep nights and workouts (TCX). The per-second sensor streams (GBs of heart rate etc.)
    are not read; the daily summary condenses them. Records the vault already has are not imported
    but written to google_fit_already_there.jsonl next to the vault.
    """
    path = os.path.expanduser(takeout_path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"Google Fit Takeout path does not exist: {path}")
    db = target_db or vault_db_path()
    os.makedirs(os.path.dirname(db), exist_ok=True)
    already_there = os.path.join(os.path.dirname(db), "google_fit_already_there.jsonl")
    open(already_there, "w").close()  # one file per import run

    reader = _TakeoutReader(path)
    days, weights, bps = _gfit_daily(reader), _gfit_weights(reader), _gfit_bps(reader)
    sleeps, workouts, dup = _split_already_there(_gfit_sleep(reader), _gfit_workouts(reader), db)
    with open(already_there, "a", encoding="utf-8") as f:
        f.writelines(json.dumps(d) + "\n" for d in dup)

    days_filled = _fill_day_gaps(days, db)
    skipped = len(dup)
    for batch in (days, weights, bps, sleeps, workouts):
        skipped += save_history(batch, target_path=db, skipped_path=already_there)
    return {
        "daily_activities_found": len(days),
        "days_heart_rate_added": days_filled,
        "weights_found": len(weights),
        "blood_pressure_found": len(bps),
        "sleep_found": len(sleeps) + sum(1 for d in dup if d["type"] == "sleep"),
        "workouts_found": len(workouts) + sum(1 for d in dup if d["type"] == "workout"),
        "already_there": skipped,
        "already_there_file": already_there,
    }


def parse_yunmai_log(log_content: str) -> List[WeightMeasurement]:
    """
    Parses Yunmai devlog file (weight_online-0.log) to extract all historical weigh-in records.
    Example record:
    WeightInfo [id=5, userId=..., deviceName=YUNMAI-SIGNAL-M1US, macNo=..., weight=63.86, bmi=21.1, timeStamp=1783509831]
    """
    results: List[WeightMeasurement] = []
    # Match WeightInfo blocks
    pattern = re.compile(
        r"WeightInfo\s*\[.*?deviceName=([^,]+).*?macNo=([^,]+).*?weight=([0-9.]+).*?bmi=([0-9.]+).*?fat=([0-9.]+).*?timeStamp=([0-9]+)",
        re.DOTALL
    )

    for match in pattern.finditer(log_content):
        dev_name = match.group(1).strip()
        mac = match.group(2).strip()
        weight_kg = float(match.group(3))
        bmi = float(match.group(4))
        fat = float(match.group(5))
        timestamp_s = int(match.group(6))

        try:
            ts = datetime.fromtimestamp(timestamp_s)
        except Exception:
            ts = datetime.now()

        meas = WeightMeasurement(
            weight_kg=round(weight_kg, 2),
            timestamp=ts,
            device_name=f"{dev_name} ({mac})",
            device_address=mac,
            bmi=bmi if bmi > 0 else None,
            body_fat_percent=fat if fat > 0 else None
        )
        results.append(meas)

    return results


def parse_vihealth_ai_txt(txt_content: str, filename: str) -> Optional[EcgRecording]:
    """
    Parses ViHealth AI exported ECG txt file:
    Format: F-0-01,125,II,323.62,<sample1>,<sample2>,...
    Filename is typically YYYYMMDDHHMMSS.txt
    """
    lines = [line.strip() for line in txt_content.splitlines() if line.strip()]
    if not lines:
        return None

    header_parts = lines[0].split(",")
    if len(header_parts) < 4:
        return None

    try:
        sample_rate = int(header_parts[1])
    except ValueError:
        sample_rate = 125

    lead = header_parts[2]

    # Parse date from filename: e.g. 20260421214647.txt -> 2026-04-21 21:46:47
    base_name = os.path.basename(filename).replace(".txt", "")
    try:
        ts = datetime.strptime(base_name, "%Y%m%d%H%M%S")
    except ValueError:
        ts = datetime.now()

    # The rest of comma-separated values are millivolt samples or integers
    samples_raw = header_parts[4:]
    samples_mv: List[float] = []
    for s in samples_raw:
        try:
            val = float(s)
            # ViHealth raw counts scale: ~0.00305 mV or direct integer counts
            samples_mv.append(round(val * 0.00305, 4))
        except ValueError:
            continue

    duration_s = round(len(samples_mv) / sample_rate, 1)

    return EcgRecording(
        samples_mv=samples_mv,
        sample_rate_hz=sample_rate,
        timestamp=ts,
        lead_name=f"Lead-{lead}",
        device_name="Viatom BP2 (ViHealth Import)",
        duration_seconds=duration_s,
        rhythm_status="Imported ECG Record"
    )


def parse_fitdays_csv(csv_content: str) -> List[WeightMeasurement]:
    """
    Parses Fitdays exported body composition CSV.
    Supports English and international headers, comma/tab/semicolon delimited.
    Columns typically: Time/Date, Weight, BMI, Body Fat (%), Subcutaneous Fat, Visceral Fat, etc.
    """
    results: List[WeightMeasurement] = []
    lines = [l.strip() for l in csv_content.splitlines() if l.strip()]
    if not lines:
        return []

    first_line = lines[0]
    delimiter = ","
    if "\t" in first_line:
        delimiter = "\t"
    elif ";" in first_line:
        delimiter = ";"

    reader = csv.reader(lines, delimiter=delimiter)
    header = None
    col_idx = {}

    for row in reader:
        if not row:
            continue
        if header is None:
            header = [c.strip().lower() for c in row]
            for idx, col in enumerate(header):
                if any(k in col for k in ("time", "date", "datum")):
                    col_idx["time"] = idx
                elif any(k in col for k in ("weight", "gewicht")):
                    col_idx["weight"] = idx
                elif "bmi" in col:
                    col_idx["bmi"] = idx
                elif any(k in col for k in ("body fat", "fat rate", "lichaamsvet")):
                    col_idx["fat"] = idx
            continue

        try:
            if "time" not in col_idx or "weight" not in col_idx:
                continue

            time_str = row[col_idx["time"]].strip()
            w_str = row[col_idx["weight"]].strip().replace("kg", "").replace("lb", "").replace(",", ".").strip()
            raw_w = float(w_str)

            is_lb = "lb" in header[col_idx["weight"]] or "lb" in row[col_idx["weight"]].lower() or raw_w > 90
            if is_lb:
                w_lb = raw_w
                w_kg = round(w_lb / 2.20462, 2)
            else:
                w_kg = raw_w
                w_lb = round(w_kg * 2.20462, 1)

            bmi = float(row[col_idx["bmi"]].strip().replace(",", ".")) if "bmi" in col_idx and row[col_idx["bmi"]].strip() else None
            fat = float(row[col_idx["fat"]].strip().replace("%", "").replace(",", ".")) if "fat" in col_idx and row[col_idx["fat"]].strip() else None

            dt = None
            for fmt in (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%Y/%m/%d %H:%M:%S",
                "%Y/%m/%d %H:%M",
                "%d/%m/%Y %H:%M:%S",
                "%d/%m/%Y %H:%M",
                "%H:%M %d/%m/%Y",
                "%m/%d/%Y %H:%M:%S",
                "%m/%d/%Y %H:%M"
            ):
                try:
                    dt = datetime.strptime(time_str, fmt)
                    break
                except Exception:
                    pass

            if not dt:
                try:
                    from dateutil import parser as dt_parser
                    dt = dt_parser.parse(time_str)
                except Exception:
                    dt = datetime.now(timezone.utc)

            meas = WeightMeasurement(
                weight_kg=w_kg,
                weight_lb=w_lb,
                bmi=bmi,
                body_fat_percent=fat,
                timestamp=dt,
                device_name="Fitdays CSV import",
            )
            results.append(meas)
        except Exception as e:
            logger.debug(f"Skipping Fitdays row: {e}")
            continue

    return results


def parse_omron_csv(csv_content: str) -> List[BloodPressureMeasurement]:
    """
    Parses standard exported Omron Connect CSV:
    Header: Date, Time, Systolic (mmHg), Diastolic (mmHg), Pulse (bpm)
    """
    results: List[BloodPressureMeasurement] = []
    reader = csv.reader(csv_content.splitlines())
    header = None

    for row in reader:
        if not row:
            continue
        if not header:
            header = [col.strip().lower() for col in row]
            continue

        try:
            # Map standard columns
            # Common omron headers: date, time, systolic, diastolic, pulse
            date_str = row[0].strip()
            time_str = row[1].strip()
            ts = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
            sys = int(row[2].strip())
            dia = int(row[3].strip())
            pulse = int(row[4].strip())

            meas = BloodPressureMeasurement(
                systolic=sys,
                diastolic=dia,
                pulse=pulse,
                timestamp=ts,
                device_name="Omron Connect (CSV Import)"
            )
            results.append(meas)
        except Exception:
            continue

    return results


def parse_vesync_data(raw_data: list, current_year: int = 2026, tz_name: Optional[str] = None) -> List[WeightMeasurement]:
    """
    Parses scraped or exported VeSync scale records:
    [{"day": "10/1" or "8/10/2025", "time": "05:32", "weight_lb": 141.0, "body_fat_percent": 15.3}, ...]
    """
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(tz_name) if tz_name else datetime.now().astimezone().tzinfo  # default: local zone
    results: List[WeightMeasurement] = []

    for d in raw_data:
        try:
            day_str = str(d.get("day", "")).strip()
            time_str = str(d.get("time", "")).strip()
            if not day_str or not time_str:
                continue

            parts = day_str.split("/")
            if len(parts) == 2:
                month, day = int(parts[0]), int(parts[1])
                year = current_year
            elif len(parts) == 3:
                month, day, year = int(parts[0]), int(parts[1]), int(parts[2])
            else:
                continue

            time_parts = time_str.split(":")
            hour = int(time_parts[0])
            minute = int(time_parts[1])
            second = int(time_parts[2]) if len(time_parts) > 2 else 0

            local_dt = datetime(year, month, day, hour, minute, second, tzinfo=tz)
            utc_dt = local_dt.astimezone(timezone.utc)

            w_lb = float(d.get("weight_lb") or 0.0)
            if w_lb <= 0:
                continue
            w_kg = round(w_lb / 2.20462, 2)
            fat = float(d["body_fat_percent"]) if d.get("body_fat_percent") is not None else None

            meas = WeightMeasurement(
                weight_kg=w_kg,
                weight_lb=w_lb,
                body_fat_percent=fat,
                timestamp=utc_dt,
                device_name="Etekcity Scaly (VeSync)",
                device_address="com.etekcity.vesyncplatform"
            )
            results.append(meas)
        except Exception as e:
            logger.debug(f"Skipping VeSync record: {e}")
            continue

    return results


def import_csv_file(file_path: str, target_db: Optional[str] = None) -> dict:
    """
    Imports a Fitdays (body composition) or Omron (blood pressure) CSV export,
    detected from its contents. Raises ValueError for any other CSV.
    """
    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    weights = parse_fitdays_csv(content)
    if weights:
        save_history(weights, target_path=target_db)
        return {"source": "Fitdays", "weights_found": len(weights)}
    bps = parse_omron_csv(content)
    if bps:
        save_history(bps, target_path=target_db)
        return {"source": "Omron", "blood_pressure_found": len(bps)}
    raise ValueError("Not a recognised Fitdays or Omron CSV export")


def import_garmin_file(path: str, target_db: Optional[str] = None) -> dict:
    """
    Imports a single Garmin .fit file (workout or sleep/HRV), or a Garmin data export (.zip or extracted folder):
    weights, daily activity and workouts, plus sleep/HRV records.
    Raises ValueError when nothing usable is found.
    """
    base_name = os.path.basename(path).lower()
    if path.lower().endswith(".fit"):
        if base_name.startswith("sleep"):
            # Try to find matching HRV file in same directory
            dir_name = os.path.dirname(path)
            hrv_candidate = None
            if dir_name:
                date_part = base_name.replace("sleep_", "").split("_")[0]
                for f in os.listdir(dir_name):
                    if f.lower().startswith("hrv") and date_part in f:
                        hrv_candidate = os.path.join(dir_name, f)
                        break
            session = parse_garmin_sleep_fit(path, hrv_candidate)
            if not session:
                raise ValueError(f"Could not parse sleep FIT file {base_name}")
            save_history([session], target_path=target_db)
            return {"sleep_records_found": 1, "session": f"{session.date}: {session.duration_hours}h, score {session.sleep_score}"}
        elif base_name.startswith("hrv"):
            raise ValueError("HRV_STATUS files are imported together with their SLEEP file "
                             "(import the folder, or the SLEEP file next to it)")
        else:
            workout = parse_garmin_fit_file(path)
            if not workout:
                raise ValueError(f"Could not parse FIT file {base_name}")
            save_history([workout], target_path=target_db)
            return {"workouts_found": 1, "workout": f"{workout.sport} - {workout.distance_km} km in {round(workout.duration_seconds / 60, 1)} min"}
    elif path.lower().endswith(".gpx"):
        workout = parse_gpx_file(path)
        if not workout:
            raise ValueError(f"Could not parse GPX file {base_name}")
        save_history([workout], target_path=target_db)
        return {"workouts_found": 1, "workout": f"{workout.sport} - {workout.distance_km} km in {round(workout.duration_seconds / 60, 1)} min"}

    res = parse_garmin_takeout(path, target_db)
    if os.path.isfile(path) and path.lower().endswith(".zip"):
        from uniscale.sleep import import_garmin_sleep_from_zip
        res["sleep_records_found"] = import_garmin_sleep_from_zip(path, db_path=target_db)
    elif os.path.isdir(path):
        sleep_files = {}
        hrv_files = {}
        for root, _, files in os.walk(path):
            for f in files:
                fl = f.lower()
                fpath = os.path.join(root, f)
                if fl.startswith("sleep_") and fl.endswith(".fit"):
                    m = re.search(r"(\d{4}-\d{2}-\d{2})", f)
                    if m:
                        sleep_files[m.group(1)] = fpath
                elif fl.startswith("hrv_status_") and fl.endswith(".fit"):
                    m = re.search(r"(\d{4}-\d{2}-\d{2})", f)
                    if m:
                        hrv_files[m.group(1)] = fpath
        sleep_sessions = []
        for date_str, s_path in sleep_files.items():
            h_path = hrv_files.get(date_str)
            s = parse_garmin_sleep_fit(s_path, h_path)
            if s:
                sleep_sessions.append(s)
        if sleep_sessions:
            save_history(sleep_sessions, target_path=target_db)
            res["sleep_records_found"] = res.get("sleep_records_found", 0) + len(sleep_sessions)

    if not any(res.values()):
        raise ValueError("No Garmin weights, activities, workouts or sleep records found in this export")
    return res


def import_vesync_file(file_path: str, target_db: Optional[str] = None) -> dict:
    """
    Imports VeSync scraped JSON into UniScale database.
    """
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, dict) and "records" in data:
        data = data["records"]
    weights = parse_vesync_data(data)
    save_history(weights, target_path=target_db)
    return {
        "total_parsed": len(weights),
        "target_db": target_db or vault_db_path()
    }


def parse_quest_pdf(pdf_path: str, tz_name: Optional[str] = None) -> Optional[BloodTestMeasurement]:
    """
    Parses a Quest Diagnostics lab report PDF using pdftotext.
    Extracts collection date, fasting status, and panel biomarkers.
    """
    if not os.path.exists(pdf_path):
        logger.error(f"File not found: {pdf_path}")
        return None

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name) if tz_name else datetime.now().astimezone().tzinfo  # default: local zone
    except Exception:
        tz = timezone.utc

    res = subprocess.run(["pdftotext", "-layout", pdf_path, "-"], capture_output=True, text=True)
    if res.returncode != 0 or not res.stdout:
        logger.error(f"pdftotext failed for {pdf_path}")
        return None

    lines = res.stdout.splitlines()

    collected_dt = None
    fasting = None

    for line in lines[:80]:
        m_col = re.search(r"Collected:\s*([0-9]{2}/[0-9]{2}/[0-9]{4}\s+[0-9]{2}:[0-9]{2})", line)
        if m_col:
            dt_str = m_col.group(1)
            try:
                local_dt = datetime.strptime(dt_str, "%m/%d/%Y %H:%M").replace(tzinfo=tz)
                collected_dt = local_dt.astimezone(timezone.utc)
            except Exception:
                pass
        if "FASTING:YES" in line.replace(" ", "").upper():
            fasting = True
        elif "FASTING:NO" in line.replace(" ", "").upper():
            fasting = False

    if not collected_dt:
        collected_dt = datetime.fromtimestamp(os.path.getmtime(pdf_path), tz=timezone.utc)

    target_markers = [
        (r"CHOLESTEROL,\s*TOTAL", "total_cholesterol", "Total Cholesterol", "lipid", "mg/dL"),
        (r"HDL\s*CHOLESTEROL", "hdl_cholesterol", "HDL Cholesterol", "lipid", "mg/dL"),
        (r"TRIGLYCERIDES", "triglycerides", "Triglycerides", "lipid", "mg/dL"),
        (r"LDL-CHOLESTEROL", "ldl_cholesterol", "LDL Cholesterol", "lipid", "mg/dL"),
        (r"CHOL/HDLC\s*RATIO", "chol_hdl_ratio", "Cholesterol / HDL Ratio", "lipid", "ratio"),
        (r"NON\s*HDL\s*CHOLESTEROL", "non_hdl_cholesterol", "Non-HDL Cholesterol", "lipid", "mg/dL"),
        (r"GLUCOSE", "glucose", "Fasting Glucose", "metabolic", "mg/dL"),
        (r"UREA\s*NITROGEN\s*\(BUN\)", "bun", "Urea Nitrogen (BUN)", "metabolic", "mg/dL"),
        (r"CREATININE", "creatinine", "Creatinine", "metabolic", "mg/dL"),
        (r"EGFR", "egfr", "eGFR", "metabolic", "mL/min/1.73m2"),
        (r"SODIUM", "sodium", "Sodium", "metabolic", "mmol/L"),
        (r"POTASSIUM", "potassium", "Potassium", "metabolic", "mmol/L"),
        (r"CHLORIDE", "chloride", "Chloride", "metabolic", "mmol/L"),
        (r"CARBON\s*DIOXIDE", "carbon_dioxide", "Carbon Dioxide", "metabolic", "mmol/L"),
        (r"CALCIUM", "calcium", "Calcium", "metabolic", "mg/dL"),
        (r"PROTEIN,\s*TOTAL", "total_protein", "Total Protein", "metabolic", "g/dL"),
        (r"ALBUMIN\b", "albumin", "Albumin", "metabolic", "g/dL"),
        (r"GLOBULIN\b", "globulin", "Globulin", "metabolic", "g/dL"),
        (r"ALBUMIN/GLOBULIN\s*RATIO", "ag_ratio", "Albumin/Globulin Ratio", "metabolic", "ratio"),
        (r"BILIRUBIN,\s*TOTAL", "total_bilirubin", "Total Bilirubin", "metabolic", "mg/dL"),
        (r"ALKALINE\s*PHOSPHATASE", "alkaline_phosphatase", "Alkaline Phosphatase (ALP)", "metabolic", "U/L"),
        (r"\bAST\b", "ast", "AST (SGOT)", "metabolic", "U/L"),
        (r"\bALT\b", "alt", "ALT (SGPT)", "metabolic", "U/L"),
        (r"\bHS[\s-]?CRP\b|C-?\s*REACTIVE\s*PROTEIN", "crp", "hs-CRP", "metabolic", "mg/L"),
        (r"\bTSH\b", "tsh", "TSH", "thyroid", "mIU/L"),
        (r"T4,\s*FREE", "free_t4", "Free T4", "thyroid", "ng/dL"),
        (r"HEMOGLOBIN\s*A1c", "hba1c", "Hemoglobin A1c", "metabolic", "%"),
        (r"WHITE\s*BLOOD\s*CELL\s*COUNT", "wbc", "White Blood Cells (WBC)", "cbc", "Thousand/uL"),
        (r"RED\s*BLOOD\s*CELL\s*COUNT", "rbc", "Red Blood Cells (RBC)", "cbc", "Million/uL"),
        (r"\bHEMOGLOBIN\b(?!\s*A1c)", "hemoglobin", "Hemoglobin", "cbc", "g/dL"),
        (r"\bHEMATOCRIT\b", "hematocrit", "Hematocrit", "cbc", "%"),
        (r"\bMCV\b", "mcv", "MCV", "cbc", "fL"),
        (r"\bMCH\b", "mch", "MCH", "cbc", "pg"),
        (r"\bMCHC\b", "mchc", "MCHC", "cbc", "g/dL"),
        (r"\bRDW\b", "rdw", "RDW", "cbc", "%"),
        (r"PLATELET\s*COUNT", "platelets", "Platelets", "cbc", "Thousand/uL"),
        (r"ABSOLUTE\s*LYMPHOCYTES", "absolute_lymphocytes", "Absolute Lymphocytes", "cbc", "cells/uL"),
        (r"(?<!ABSOLUTE\s)\bLYMPHOCYTES\b", "lymphocytes", "Lymphocytes", "cbc", "%"),
        (r"ABSOLUTE\s*NEUTROPHILS", "absolute_neutrophils", "Absolute Neutrophils", "cbc", "cells/uL"),
        (r"(?<!ABSOLUTE\s)\bNEUTROPHILS\b", "neutrophils", "Neutrophils", "cbc", "%"),
        (r"ABSOLUTE\s*MONOCYTES", "absolute_monocytes", "Absolute Monocytes", "cbc", "cells/uL"),
        (r"(?<!ABSOLUTE\s)\bMONOCYTES\b", "monocytes", "Monocytes", "cbc", "%"),
        (r"ABSOLUTE\s*EOSINOPHILS", "absolute_eosinophils", "Absolute Eosinophils", "cbc", "cells/uL"),
        (r"(?<!ABSOLUTE\s)\bEOSINOPHILS\b", "eosinophils", "Eosinophils", "cbc", "%"),
        (r"ABSOLUTE\s*BASOPHILS", "absolute_basophils", "Absolute Basophils", "cbc", "cells/uL"),
        (r"(?<!ABSOLUTE\s)\bBASOPHILS\b", "basophils", "Basophils", "cbc", "%"),
    ]

    found_markers: Dict[str, LabMarker] = {}

    for i, line in enumerate(lines):
        for pattern, key, disp_name, cat, def_unit in target_markers:
            if key in found_markers:
                continue
            m_match = re.search(pattern, line, re.IGNORECASE)
            if m_match:
                val = None
                flag = "Normal"
                ref_range = None
                unit = def_unit

                after = line[m_match.end():]
                val_m = re.search(r"\b([0-9]+(?:\.[0-9]+)?)\s*([HL])?\b", after)
                if val_m:
                    val = float(val_m.group(1))
                    if val_m.group(2):
                        flag = val_m.group(2)
                else:
                    if i + 1 < len(lines):
                        val_m2 = re.search(r"\b([0-9]+(?:\.[0-9]+)?)\s*([HL])?\b", lines[i+1])
                        if val_m2 and "Reference" not in lines[i+1]:
                            val = float(val_m2.group(1))
                            if val_m2.group(2):
                                flag = val_m2.group(2)

                block = " ".join(lines[i:min(i+3, len(lines))])
                ref_m = re.search(r"Reference Range:\s*([^|;\n\r]+)", block, re.IGNORECASE)
                if ref_m:
                    raw_ref = ref_m.group(1).strip()
                    parts = re.split(r"\s{2,}", raw_ref)
                    cleaned = parts[0].strip()
                    # Page headers carry the patient name as "SURNAME, GIVEN"; cut there.
                    cleaned = re.split(r"\b[A-Z][A-Z'-]+,\s*[A-Z]", cleaned)[0].strip()
                    for stop_word in ["For ", "ALBUMIN", "GLOBULIN", "SEE NOTE"]:
                        if stop_word in cleaned:
                            cleaned = cleaned.split(stop_word)[0].strip()
                    ref_range = cleaned if cleaned else None

                if val is not None:
                    found_markers[key] = LabMarker(
                        name=disp_name,
                        value=val,
                        unit=unit,
                        reference_range=ref_range,
                        flag=flag,
                        category=cat
                    )

    if not found_markers:
        return None

    return BloodTestMeasurement(
        timestamp=collected_dt,
        lab_name="Quest Diagnostics",
        panel_name="Comprehensive Metabolic & Lipid Panel",
        fasting=fasting,
        markers=found_markers,
        notes=f"Parsed from {os.path.basename(pdf_path)}"
    )


def import_lab_file(file_path: str, target_db: Optional[str] = None) -> dict:
    """
    Imports a Quest Diagnostics PDF lab report or JSON into UniScale database.
    """
    meas = None
    if file_path.lower().endswith(".pdf"):
        meas = parse_quest_pdf(file_path)
    elif file_path.lower().endswith(".json"):
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        meas = BloodTestMeasurement(**data)

    if not meas:
        raise ValueError(f"Could not parse lab report from {file_path}")

    save_history([meas], target_path=target_db)
    return {
        "timestamp": meas.timestamp.isoformat(),
        "lab_name": meas.lab_name,
        "total_markers": len(meas.markers),
        "target_db": target_db or vault_db_path()
    }


def import_from_adb_device() -> dict:
    """
    Directly extracts databases and files from connected ADB phone.
    """
    summary = {
        "yunmai_records": 0,
        "vihealth_ecgs": 0,
        "errors": []
    }

    # 1. Extract Yunmai log
    try:
        res = subprocess.run(
            ["adb", "shell", "cat", "/sdcard/Android/data/com.yunmai.scaleen/files/yunmai/devlog/weight_online-0.log"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if res.returncode == 0 and res.stdout:
            yunmai_records = parse_yunmai_log(res.stdout)
            summary["yunmai_records"] = len(yunmai_records)
            save_history(yunmai_records)
    except Exception as e:
        summary["errors"].append(f"Yunmai import error: {e}")

    # 2. Extract ViHealth ECG files
    try:
        ls_res = subprocess.run(
            ["adb", "shell", "ls", "/sdcard/Android/data/com.viatom.vihealth/files/2/ai/"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if ls_res.returncode == 0:
            files = [f.strip() for f in ls_res.stdout.splitlines() if f.strip().endswith(".txt")]
            imported_ecgs = []
            for f in files:
                cat_res = subprocess.run(
                    ["adb", "shell", "cat", f"/sdcard/Android/data/com.viatom.vihealth/files/2/ai/{f}"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if cat_res.returncode == 0:
                    rec = parse_vihealth_ai_txt(cat_res.stdout, f)
                    if rec:
                        imported_ecgs.append(rec)
            summary["vihealth_ecgs"] = len(imported_ecgs)
            save_history(imported_ecgs)
    except Exception as e:
        summary["errors"].append(f"ViHealth import error: {e}")

    return summary


def _parse_epoch(val: Any) -> Optional[float]:
    if not val:
        return None
    val_str = str(val).replace("Z", "+00:00")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d"
    ):
        try:
            return datetime.strptime(val_str[:25], fmt).timestamp()
        except Exception:
            pass
    try:
        from dateutil import parser as dt_parser
        return dt_parser.parse(val_str).timestamp()
    except Exception:
        return None


def _is_primary_device(device_name: str, device_address: str) -> bool:
    dev = (str(device_name) + " " + str(device_address)).lower()
    return any(k in dev for k in ("yunmai", "fitdays", "omron", "viatom", "bp2", "forerunner", "garmin", "etekcity", "vesync"))


def save_history(records: list, target_path: Optional[str] = None, skipped_path: Optional[str] = None) -> int:
    """
    Saves health records to JSONL history with smart deduplication.
    Prefers native hardware devices (Yunmai, Fitdays, Omron, BP2, Garmin FIT) over
    secondary aggregators (Google Fit Takeout), preserving richer metrics.
    Records skipped as already present are appended to skipped_path (if given); returns their count.
    """
    if not records:
        return 0
    log_file = target_path or vault_db_path()
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    existing_keys = set()
    existing_activities = {}   # date -> max_steps
    existing_weights = []      # (epoch_s, weight_kg, has_fat_or_bmi, is_primary)
    existing_bps = []          # (epoch_s, sys, dia, is_primary)
    existing_workouts = []     # (epoch_s, sport)

    if os.path.exists(log_file):
        with open(log_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    t = d.get("type") or ("weight" if "weight_kg" in d else ("workout" if "sport" in d else ("activity" if "steps" in d else "other")))
                    ts_str = str(d.get("timestamp") or d.get("date") or "")
                    if t and ts_str:
                        existing_keys.add(f"{t}:{ts_str[:19]}")
                    if t == "sleep":
                        existing_keys.add(f"sleep:{d.get('date')}:{d.get('duration_seconds')}:{d.get('device_name')}")

                    ep = _parse_epoch(ts_str)
                    is_prim = _is_primary_device(d.get("device_name", ""), d.get("device_address", ""))

                    if t == "activity":
                        date_k = str(d.get("date", ""))[:10]
                        steps_val = int(d.get("steps") or 0)
                        if date_k:
                            existing_activities[date_k] = max(steps_val, existing_activities.get(date_k, 0))
                    elif t == "weight" and ep is not None:
                        w_kg = float(d.get("weight_kg", 0) or 0)
                        has_fat = d.get("body_fat_percent") is not None or d.get("bmi") is not None
                        existing_weights.append((ep, w_kg, has_fat, is_prim))
                    elif t == "blood_pressure" and ep is not None:
                        sys_v = int(d.get("systolic", 0) or 0)
                        dia_v = int(d.get("diastolic", 0) or 0)
                        existing_bps.append((ep, sys_v, dia_v, is_prim))
                    elif t == "workout" and ep is not None:
                        sport_v = str(d.get("sport", "")).lower()
                        existing_workouts.append((ep, sport_v))
                except Exception:
                    pass

    skipped: list = []
    with open(log_file, "a", encoding="utf-8") as f:
        for r in records:
            d = r.model_dump(mode="json")
            if isinstance(r, WeightMeasurement):
                d["type"] = "weight"
                ts = str(d.get("timestamp", ""))[:19]
            elif isinstance(r, BloodPressureMeasurement):
                d["type"] = "blood_pressure"
                ts = str(d.get("timestamp", ""))[:19]
            elif isinstance(r, EcgRecording):
                d["type"] = "ecg"
                ts = str(d.get("timestamp", ""))[:19]
            elif isinstance(r, DailyActivity):
                d["type"] = "activity"
                ts = str(d.get("date", ""))[:10]
            elif isinstance(r, ActivityWorkout):
                d["type"] = "workout"
                ts = str(d.get("timestamp", ""))[:19]
            elif isinstance(r, BloodTestMeasurement):
                d["type"] = "blood_test"
                ts = str(d.get("timestamp", ""))[:19]
            elif isinstance(r, SleepSession):
                d["type"] = "sleep"
                ts = str(d.get("timestamp", ""))[:19]
            else:
                ts = str(d.get("timestamp") or d.get("date") or "")[:19]

            t = d.get("type")
            ep = _parse_epoch(d.get("timestamp") or d.get("date"))
            is_prim = _is_primary_device(d.get("device_name", ""), d.get("device_address", ""))

            # 1. Exact string key deduplication
            key = f"{t}:{ts}"
            if key in existing_keys:
                skipped.append(d)
                continue

            # 2. Activity deduplication: skip if existing has >= steps
            if t == "activity":
                date_k = str(d.get("date", ""))[:10]
                new_steps = int(d.get("steps") or 0)
                if date_k in existing_activities and new_steps <= existing_activities[date_k]:
                    skipped.append(d)
                    continue
                existing_activities[date_k] = max(new_steps, existing_activities.get(date_k, 0))

            # 3. Weight smart deduplication
            elif t == "weight" and ep is not None:
                new_w = float(d.get("weight_kg", 0) or 0)
                new_fat = d.get("body_fat_percent") is not None or d.get("bmi") is not None
                skip = False
                for (e_ep, e_w, e_fat, e_prim) in existing_weights:
                    if abs(ep - e_ep) < 3600 and abs(new_w - e_w) < 0.5:
                        # Nearby weigh-in match within 1 hour
                        if not is_prim and e_prim:
                            skip = True
                            break
                        if not new_fat and e_fat:
                            skip = True
                            break
                        if abs(ep - e_ep) < 120:
                            skip = True
                            break
                if skip:
                    skipped.append(d)
                    continue
                existing_weights.append((ep, new_w, new_fat, is_prim))

            # 4. Blood pressure smart deduplication
            elif t == "blood_pressure" and ep is not None:
                new_sys = int(d.get("systolic", 0) or 0)
                new_dia = int(d.get("diastolic", 0) or 0)
                skip = False
                for (e_ep, e_sys, e_dia, e_prim) in existing_bps:
                    if abs(ep - e_ep) < 1800 and abs(new_sys - e_sys) <= 3 and abs(new_dia - e_dia) <= 3:
                        if not is_prim and e_prim:
                            skip = True
                            break
                        if abs(ep - e_ep) < 120:
                            skip = True
                            break
                if skip:
                    skipped.append(d)
                    continue
                existing_bps.append((ep, new_sys, new_dia, is_prim))

            # 5. Workout smart deduplication
            elif t == "workout" and ep is not None:
                new_sport = str(d.get("sport", "")).lower()
                skip = False
                for (e_ep, e_sport) in existing_workouts:
                    if abs(ep - e_ep) < 1200 and new_sport == e_sport:
                        skip = True
                        break
                if skip:
                    skipped.append(d)
                    continue
                existing_workouts.append((ep, new_sport))

            # 6. Blood test deduplication (same day deduplication)
            elif t == "blood_test":
                day_k = f"blood_test:{ts[:10]}"
                if day_k in existing_keys:
                    skipped.append(d)
                    continue
                existing_keys.add(day_k)

            # 7. Sleep: the same session (night + length + device) only once; different
            # records of one night are all kept and merged when read (uniscale.sleep.dedupe_sleep).
            elif t == "sleep":
                night_k = f"sleep:{d.get('date')}:{d.get('duration_seconds')}:{d.get('device_name')}"
                if night_k in existing_keys:
                    skipped.append(d)
                    continue
                existing_keys.add(night_k)

            f.write(json.dumps(d) + "\n")
            existing_keys.add(key)

    if skipped_path and skipped:
        with open(skipped_path, "a", encoding="utf-8") as f:
            for d in skipped:
                f.write(json.dumps(d) + "\n")
    return len(skipped)
