import json
import logging
import os
import zipfile
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from uniscale.models import SleepSession
from uniscale.config import vault_db_path, vault_locked

logger = logging.getLogger(__name__)

# A session shorter than this fraction of the longest session that day is a nap,
# kept separately instead of being merged into the night.
NAP_RATIO = 0.5


def sleep_date(r: Dict[str, Any]) -> str:
    return str(r.get("date") or str(r.get("timestamp") or "")[:10])


def _duration(r: Dict[str, Any]) -> int:
    return r.get("duration_seconds") or 0


def _rank(r: Dict[str, Any]) -> tuple:
    """Richer record wins: has stage data, then a device-reported score, then longer."""
    staged = (r.get("deep_sleep_seconds") or 0) + (r.get("rem_sleep_seconds") or 0) > 0
    return (staged, r.get("sleep_score") is not None, _duration(r))


def merge_sleep_records(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Merges two records of the same night: the richer one wins, the other only fills its gaps."""
    primary, other = (a, b) if _rank(a) >= _rank(b) else (b, a)
    merged = dict(primary)
    for k, v in other.items():
        if v not in (None, 0, "") and merged.get(k) in (None, 0, ""):
            merged[k] = v
    return merged


def dedupe_sleep(records: List[Dict[str, Any]]) -> tuple:
    """Returns (one merged record per night, naps marked nap=True)."""
    nights: Dict[str, Dict[str, Any]] = {}
    naps: List[Dict[str, Any]] = []
    for r in records:
        dt = sleep_date(r)
        if not dt:
            continue
        night = nights.get(dt)
        if night is None:
            nights[dt] = r
            continue
        shorter, longer = sorted((night, r), key=_duration)
        if _duration(shorter) < NAP_RATIO * _duration(longer):
            nights[dt] = longer
            naps.append({**shorter, "nap": True})
        else:
            nights[dt] = merge_sleep_records(night, r)
    return list(nights.values()), naps


@vault_locked


def import_garmin_sleep_from_zip(
    zip_path: str,
    db_path: Optional[str] = None
) -> int:
    """
    Parses all Garmin sleepData.json and healthStatusData.json files from
    a Garmin data export ZIP file, merges sleep architecture with nocturnal HRV/SpO2,
    and appends deduped records to measurements.jsonl.
    """
    if not os.path.exists(zip_path):
        raise FileNotFoundError(f"Export zip not found: {zip_path}")

    target_db = db_path or vault_db_path()

    # Step 1: Extract HRV from healthStatusData files
    hrv_by_date: Dict[str, Dict[str, Any]] = {}
    sleep_by_date: Dict[str, Dict[str, Any]] = {}

    with zipfile.ZipFile(zip_path, "r") as z:
        for fname in z.namelist():
            if "healthStatusData.json" in fname:
                try:
                    entries = json.loads(z.read(fname).decode("utf-8"))
                    for entry in entries:
                        c_date = entry.get("calendarDate")
                        if not c_date:
                            continue
                        metrics = {m.get("type"): m for m in entry.get("metrics", [])}
                        hrv_info = metrics.get("HRV")
                        if hrv_info:
                            hrv_by_date[c_date] = {
                                "hrv_rmssd": hrv_info.get("value"),
                                "status": hrv_info.get("status"),
                                "baseline_low": hrv_info.get("baselineLowerLimit"),
                                "baseline_high": hrv_info.get("baselineUpperLimit"),
                            }
                except Exception as e:
                    logger.debug(f"Error parsing healthStatus {fname}: {e}")

        # Step 2: Extract sleepData
        for fname in z.namelist():
            if "sleepData.json" in fname:
                try:
                    entries = json.loads(z.read(fname).decode("utf-8"))
                    for entry in entries:
                        c_date = entry.get("calendarDate")
                        if not c_date:
                            continue
                        deep_s = entry.get("deepSleepSeconds", 0)
                        light_s = entry.get("lightSleepSeconds", 0)
                        rem_s = entry.get("remSleepSeconds", 0)
                        awake_s = entry.get("awakeSleepSeconds", 0)

                        tot_s = deep_s + light_s + rem_s
                        if tot_s <= 0:
                            continue

                        scores = entry.get("sleepScores") or {}
                        spo2 = entry.get("spo2SleepSummary") or {}

                        # Keep the most complete entry for this date
                        if c_date not in sleep_by_date or tot_s > sleep_by_date[c_date]["tot_s"]:
                            sleep_by_date[c_date] = {
                                "tot_s": tot_s,
                                "deep_s": deep_s,
                                "light_s": light_s,
                                "rem_s": rem_s,
                                "awake_s": awake_s,
                                "start_gmt": entry.get("sleepStartTimestampGMT"),
                                "end_gmt": entry.get("sleepEndTimestampGMT"),
                                "sleep_score": scores.get("overallScore"),
                                "quality_score": scores.get("qualityScore"),
                                "recovery_score": scores.get("recoveryScore"),
                                "deep_score": scores.get("deepScore"),
                                "rem_score": scores.get("remScore"),
                                "avg_spo2": spo2.get("averageSPO2"),
                                "lowest_spo2": spo2.get("lowestSPO2"),
                                "avg_respiration": entry.get("averageRespiration"),
                                "avg_sleep_stress": entry.get("avgSleepStress"),
                            }
                except Exception as e:
                    logger.debug(f"Error parsing sleepData {fname}: {e}")

    # Read existing dates from target_db to prevent duplicate insertions
    existing_sleep_dates = set()
    if os.path.exists(target_db):
        with open(target_db, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                    if d.get("type") == "sleep":
                        c_date = d.get("date") or str(d.get("timestamp", ""))[:10]
                        if c_date:
                            existing_sleep_dates.add(c_date)
                except Exception:
                    pass

    inserted = 0
    os.makedirs(os.path.dirname(os.path.abspath(target_db)), exist_ok=True)
    with open(target_db, "a", encoding="utf-8") as f:
        for c_date in sorted(sleep_by_date.keys()):
            if c_date in existing_sleep_dates:
                continue

            s = sleep_by_date[c_date]
            h = hrv_by_date.get(c_date, {})

            ts = s["end_gmt"] or f"{c_date}T08:00:00Z"
            if len(ts) == 19:
                ts += "Z"

            record = {
                "timestamp": ts,
                "date": c_date,
                "duration_seconds": s["tot_s"],
                "duration_hours": round(s["tot_s"] / 3600.0, 2),
                "deep_sleep_seconds": s["deep_s"],
                "deep_sleep_minutes": round(s["deep_s"] / 60.0),
                "rem_sleep_seconds": s["rem_s"],
                "rem_sleep_minutes": round(s["rem_s"] / 60.0),
                "light_sleep_seconds": s["light_s"],
                "light_sleep_minutes": round(s["light_s"] / 60.0),
                "awake_seconds": s["awake_s"],
                "awake_minutes": round(s["awake_s"] / 60.0),
                "sleep_score": s["sleep_score"],
                "quality_score": s["quality_score"],
                "recovery_score": s["recovery_score"],
                "deep_score": s["deep_score"],
                "rem_score": s["rem_score"],
                "avg_spo2": round(s["avg_spo2"], 1) if s["avg_spo2"] is not None else None,
                "lowest_spo2": s["lowest_spo2"],
                "avg_respiration": round(s["avg_respiration"], 1) if s["avg_respiration"] is not None else None,
                "avg_sleep_stress": round(s["avg_sleep_stress"], 1) if s["avg_sleep_stress"] is not None else None,
                "hrv_rmssd_ms": h.get("hrv_rmssd"),
                "hrv_status": h.get("status"),
                "device_name": "Garmin",
                "type": "sleep"
            }

            f.write(json.dumps(record) + "\n")
            inserted += 1

    logger.info(f"Imported {inserted} new sleep records from Garmin export.")
    return inserted


def get_sleep_summary(
    db_path: Optional[str] = None,
    days: int = 14
) -> Dict[str, Any]:
    """
    Assembles sleep architecture, nocturnal SpO2, and overnight HRV trends.
    """
    path = db_path or vault_db_path()
    records: List[Dict[str, Any]] = []

    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if d.get("type") == "sleep":
                        records.append(d)
                except Exception:
                    pass

    records, _naps = dedupe_sleep(records)

    # Sort chronological
    records.sort(key=lambda x: str(x.get("date") or x.get("timestamp") or ""))

    if not records:
        return {
            "latest": None,
            "trend": [],
            "averages_7d": None,
            "recovery_status": "No Data"
        }

    latest = records[-1]
    recent = records[-days:]

    # 7-day averages
    recent_7 = records[-7:]
    valid_scores = [r["sleep_score"] for r in recent_7 if r.get("sleep_score")]
    valid_dur = [r["duration_seconds"] for r in recent_7 if r.get("duration_seconds")]
    valid_deep = [r["deep_sleep_seconds"] for r in recent_7 if r.get("deep_sleep_seconds")]
    valid_rem = [r["rem_sleep_seconds"] for r in recent_7 if r.get("rem_sleep_seconds")]
    valid_hrv = [r["hrv_rmssd_ms"] for r in recent_7 if r.get("hrv_rmssd_ms")]
    valid_spo2 = [r["avg_spo2"] for r in recent_7 if r.get("avg_spo2")]

    avg_score = round(sum(valid_scores) / len(valid_scores)) if valid_scores else None
    avg_dur_hrs = round((sum(valid_dur) / len(valid_dur)) / 3600.0, 1) if valid_dur else 0.0

    avg_deep_s = sum(valid_deep) / len(valid_deep) if valid_deep else 0
    avg_rem_s = sum(valid_rem) / len(valid_rem) if valid_rem else 0
    tot_s = sum(valid_dur) / len(valid_dur) if valid_dur else 1

    deep_pct = round((avg_deep_s / tot_s) * 100.0, 1) if tot_s > 0 else 0.0
    rem_pct = round((avg_rem_s / tot_s) * 100.0, 1) if tot_s > 0 else 0.0
    avg_hrv = round(sum(valid_hrv) / len(valid_hrv), 1) if valid_hrv else None
    avg_spo2 = round(sum(valid_spo2) / len(valid_spo2), 1) if valid_spo2 else None

    # Determine recovery status
    status = "Optimal Recovery"
    if avg_score and avg_score < 65:
        status = "Suboptimal / High Fatigue"
    elif avg_score and avg_score < 75:
        status = "Moderate / Balanced"

    return {
        "latest": latest,
        "trend": recent,
        "averages_7d": {
            "avg_sleep_score": avg_score,
            "avg_duration_hours": avg_dur_hrs,
            "deep_sleep_percent": deep_pct,
            "rem_sleep_percent": rem_pct,
            "avg_hrv_rmssd_ms": avg_hrv,
            "avg_spo2_percent": avg_spo2,
        },
        "recovery_status": status,
        "total_nights_recorded": len(records)
    }
