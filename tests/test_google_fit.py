"""Google Fit Takeout import (read in place from the zip, duplicates to the already-there file) and
read-time merging of duplicate weight / blood-pressure readings."""
import json
import zipfile

from uniscale.importer import parse_google_fit_takeout
from uniscale.readings import merge_duplicate_readings

T0 = 1762000000  # 2025-11-01T12:26:40Z


def _points(*pts):
    return json.dumps({"Data Points": [
        {"fitValue": [{"value": v} for v in vals], "startTimeNanos": int(s * 1e9), "endTimeNanos": int(e * 1e9)}
        for s, e, vals in pts]})


def _takeout(tmp_path):
    fit = "Takeout/Fit/"
    data = fit + "All Data/"
    path = tmp_path / "takeout.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(fit + "Daily activity metrics/Daily activity metrics.csv",
                   "Date,Step count,Distance (m),Calories (kcal),Average heart rate (bpm),"
                   "Min heart rate (bpm),Max heart rate (bpm),Move Minutes count\n"
                   "1999-12-31,41,0,0,61,61,61,\n"  # device-clock junk
                   "2025-11-01,9000,6500.0,2100.5,72.4,50,150,45\n")
        z.writestr(data + "derived_com.google.weight_com.google.android.gms_merge_weight.json",
                   _points((T0, T0, [{"fpVal": 80.0}]), (T0 + 86400, T0 + 86400, [{"fpVal": 79.6}])))
        z.writestr(data + "derived_com.google.blood_pressure_com.google.android.gms_merged.json",
                   _points((T0, T0, [{"fpVal": 120}, {"fpVal": 80}, {}, {}])))
        z.writestr(data + "derived_com.google.sleep.segment_com.google.android.gms_merged.json",
                   _points((T0, T0 + 3600, [{"intVal": 4}]), (T0 + 3600, T0 + 5400, [{"intVal": 5}]),
                           (T0 + 5400, T0 + 5700, [{"intVal": 1}])))
        z.writestr(fit + "Activities/2025-11-01T07_00_00-05_00_PT30M_Treadmill running.tcx",
                   '<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2">'
                   '<Activities><Activity Sport="Running"><Lap StartTime="2025-11-01T12:00:00.000Z">'
                   "<TotalTimeSeconds>1800</TotalTimeSeconds><DistanceMeters>5000</DistanceMeters>"
                   "<Calories>300</Calories><AverageHeartRateBpm><Value>140</Value></AverageHeartRateBpm>"
                   "<MaximumHeartRateBpm><Value>165</Value></MaximumHeartRateBpm></Lap></Activity></Activities>"
                   "</TrainingCenterDatabase>")
    return path


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_google_fit_import_and_already_there(tmp_path):
    db = tmp_path / "vault" / "measurements.jsonl"
    db.parent.mkdir()
    # The vault already has the first weigh-in from the scale itself.
    db.write_text(json.dumps({"type": "weight", "weight_kg": 80.0, "timestamp": "2025-11-01T12:30:00+00:00",
                              "device_name": "Yunmai", "device_address": "x"}) + "\n")

    res = parse_google_fit_takeout(str(_takeout(tmp_path)), target_db=str(db))

    assert res["daily_activities_found"] == 1 and res["weights_found"] == 2
    assert res["sleep_found"] == 1 and res["workouts_found"] == 1 and res["already_there"] == 1
    rows = _rows(db)
    day = next(r for r in rows if r["type"] == "activity")
    assert (day["steps"], day["avg_heart_rate"], day["min_heart_rate"], day["max_heart_rate"]) == (9000, 72, 50, 150)
    assert sum(r["type"] == "weight" for r in rows) == 2  # the scale's + the new day's
    sleep = next(r for r in rows if r["type"] == "sleep")
    assert (sleep["duration_seconds"], sleep["deep_sleep_seconds"], sleep["awake_seconds"]) == (5400, 1800, 300)
    run = next(r for r in rows if r["type"] == "workout")
    assert (run["sport"], run["sub_sport"], run["avg_heart_rate"]) == ("running", "treadmill", 140)
    dup = _rows(db.parent / "google_fit_already_there.jsonl")
    assert [(d["type"], d["weight_kg"]) for d in dup] == [("weight", 80.0)]

    # Importing again: everything is already there, nothing new lands in the vault.
    before = len(_rows(db))
    again = parse_google_fit_takeout(str(_takeout(tmp_path)), target_db=str(db))
    assert len(_rows(db)) == before and again["already_there"] == 6 and again["days_heart_rate_added"] == 0


def test_heart_rate_added_to_a_day_the_vault_already_has(tmp_path):
    from uniscale.readings import load_vault
    db = tmp_path / "measurements.jsonl"
    db.write_text(json.dumps({"type": "activity", "date": "2025-11-01", "steps": 12000, "distance_km": 9.0,
                              "source": "Garmin"}) + "\n")

    res = parse_google_fit_takeout(str(_takeout(tmp_path)), target_db=str(db))

    assert res["days_heart_rate_added"] == 1
    day = next(r for r in load_vault(str(db)) if r["type"] == "activity")
    assert (day["steps"], day["source"], day["avg_heart_rate"], day["max_heart_rate"]) == (12000, "Garmin", 72, 150)
    assert parse_google_fit_takeout(str(_takeout(tmp_path)), target_db=str(db))["days_heart_rate_added"] == 0


def test_duplicate_readings_merge():
    def bp(ts, s, d, p):
        return {"type": "blood_pressure", "timestamp": ts, "systolic": s, "diastolic": d, "pulse": p}

    got = merge_duplicate_readings([
        {"type": "weight", "timestamp": "2026-11-01T07:00:00+00:00", "weight_kg": 80.0, "body_fat_percent": 20.1},
        {"type": "weight", "timestamp": "2026-11-01T07:00:41Z", "weight_kg": 80.1},
        {"type": "weight", "timestamp": "2026-11-01T19:00:00+00:00", "weight_kg": 80.0},
        bp("2026-11-01T08:00:00+00:00", 120, 80, 0),
        bp("2026-11-01T08:00:05+00:00", 121, 80, 64),
        bp("2026-11-01T08:02:00+00:00", 120, 79, 0),
        bp("2026-11-01T09:00:00+00:00", 118, 78, 0),
    ])
    weights = [r for r in got if r["type"] == "weight"]
    bps = [r for r in got if r["type"] == "blood_pressure"]
    assert len(weights) == 2 and weights[0]["body_fat_percent"] == 20.1
    assert [b["pulse"] for b in bps] == [64, None]
