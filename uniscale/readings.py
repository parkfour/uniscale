"""
Collapses near-duplicate weight and blood-pressure readings.

One measurement often reaches the vault several times: live from the device, again when the
phone re-reads Health Connect (no pulse, slightly different timestamp), and again from the
vendor app's own Health Connect copy. Copies of one reading are merged into one record:
the most detailed copy wins and the others only fill its gaps. A pulse of 0 counts as missing.
"""
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from uniscale.sleep import dedupe_sleep

SAME_READING_WINDOW_S = 600  # copies of one reading land within minutes of each other
WEIGHT_TOLERANCE_KG = 0.3
BP_TOLERANCE_MMHG = 3
_EMPTY = (None, 0, 0.0, "")


def _epoch(r: Dict[str, Any]) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(r.get("timestamp"))).timestamp()
    except ValueError:
        return None


def _same_reading(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    if a.get("type") != b.get("type"):
        return False
    if a.get("type") == "weight":
        return abs(float(a.get("weight_kg") or 0) - float(b.get("weight_kg") or 0)) < WEIGHT_TOLERANCE_KG
    return (abs(int(a.get("systolic") or 0) - int(b.get("systolic") or 0)) <= BP_TOLERANCE_MMHG
            and abs(int(a.get("diastolic") or 0) - int(b.get("diastolic") or 0)) <= BP_TOLERANCE_MMHG)


def _detail(r: Dict[str, Any]) -> int:
    return sum(1 for v in r.values() if v not in _EMPTY)


def _merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    primary, other = (a, b) if _detail(a) >= _detail(b) else (b, a)
    merged = dict(primary)
    for k, v in other.items():
        if v not in _EMPTY and merged.get(k) in _EMPTY:
            merged[k] = v
    return merged


def merge_duplicate_readings(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """records: weight and/or blood_pressure dicts (with "type"). Returns them de-duplicated."""
    timed = sorted(((_epoch(r), r) for r in records), key=lambda x: (x[0] is None, x[0] or 0))
    out: List[list] = []  # [epoch, record]
    for ep, r in timed:
        match = None
        if ep is not None:
            # ponytail: linear look-back over the window; fine for a personal vault
            for slot in reversed(out):
                if slot[0] is None or ep - slot[0] > SAME_READING_WINDOW_S:
                    break
                if _same_reading(slot[1], r):
                    match = slot
                    break
        if match:
            match[1] = _merge(match[1], r)
        else:
            out.append([ep, r])
    result = [r for _, r in out]
    for r in result:
        if r.get("type") == "blood_pressure" and not r.get("pulse"):
            r["pulse"] = None
    return result



def load_vault(db_path: str) -> List[Dict[str, Any]]:
    """
    Every record of a vault, de-duplicated and sorted by time: one record per activity day (fields merged),
    one per sleep night (+ naps), one per weight / BP reading. Everything that reads the vault uses this.
    """
    if not os.path.exists(db_path):
        return []
    raw = []
    with open(db_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    raw.append(json.loads(line))
                except Exception:
                    pass

    records: List[Dict[str, Any]] = []
    seen_keys: Dict[str, int] = {}
    sleep_raw: List[Dict[str, Any]] = []
    readings_raw: List[Dict[str, Any]] = []

    for r in raw:
        t = r.get("type")
        if not t:
            if "weight_kg" in r:
                t = "weight"
            elif "systolic" in r:
                t = "blood_pressure"
            elif "samples_mv" in r:
                t = "ecg"
            elif "sport" in r:  # before "steps": workouts carry steps too
                t = "workout"
            elif "steps" in r:
                t = "activity"
            elif "markers" in r:
                t = "blood_test"
            else:
                t = "other"
            r["type"] = t

        if t == "activity":
            key = f"activity:{r.get('date')}"
            if key in seen_keys:
                idx = seen_keys[key]
                existing = records[idx]
                merged_steps = max(int(r.get("steps") or 0), int(existing.get("steps") or 0))
                merged_dist = max(float(r.get("distance_km") or 0.0), float(existing.get("distance_km") or 0.0))
                ex_src = str(existing.get("source") or "")
                new_src = str(r.get("source") or "")
                merged_src = ex_src if "Garmin" in ex_src else (new_src if "Garmin" in new_src else (new_src or ex_src))

                # Every other field: the newer record's value wins, the older one fills its gaps.
                records[idx] = {
                    **existing,
                    **{k: v for k, v in r.items() if v},
                    "steps": merged_steps,
                    "distance_km": round(merged_dist, 2),
                    "source": merged_src,
                    "type": "activity"
                }
                continue
        elif t == "sleep":
            sleep_raw.append(r)
            continue
        elif t in ("weight", "blood_pressure"):
            readings_raw.append(r)
            continue
        elif t in ("ecg", "workout", "blood_test"):
            ts_str = str(r.get("timestamp") or "")[:19].replace("T", " ")
            key = f"{t}:{ts_str}"
            if key and key in seen_keys:
                if t == "workout":  # re-sent with more detail (splits, HR...): the richer copy wins
                    records[seen_keys[key]] = _merge(records[seen_keys[key]], r)
                continue
        else:
            key = None

        if key:
            seen_keys[key] = len(records)
        records.append(r)

    nights, naps = dedupe_sleep(sleep_raw)
    records.extend(nights + naps)

    records.extend(merge_duplicate_readings(readings_raw))

    def get_sort_key(rec: Dict[str, Any]) -> str:
        val = str(rec.get("timestamp") or rec.get("date") or "")
        return val[:19].replace(" ", "T")

    records.sort(key=get_sort_key)
    return records


if __name__ == "__main__":
    def w(ts, kg, **kw):
        return {"type": "weight", "timestamp": ts, "weight_kg": kg, **kw}

    def bp(ts, s, d, p):
        return {"type": "blood_pressure", "timestamp": ts, "systolic": s, "diastolic": d, "pulse": p}

    got = merge_duplicate_readings([
        w("2026-11-01T07:00:00+00:00", 80.0, body_fat_percent=20.1),
        w("2026-11-01T07:00:41Z", 80.1),                 # Health Connect copy
        w("2026-11-01T19:00:00+00:00", 80.0),            # same weight hours later: a new reading
        bp("2026-11-01T08:00:00+00:00", 120, 80, 0),     # HC copy, no pulse
        bp("2026-11-01T08:00:05+00:00", 121, 80, 64),    # device reading
        bp("2026-11-01T08:02:00+00:00", 120, 79, 0),     # vendor app copy
        bp("2026-11-01T09:00:00+00:00", 118, 78, 0),     # lone reading without pulse
    ])
    weights = [r for r in got if r["type"] == "weight"]
    bps = [r for r in got if r["type"] == "blood_pressure"]
    assert len(weights) == 2 and weights[0]["body_fat_percent"] == 20.1, weights
    assert len(bps) == 2 and bps[0]["pulse"] == 64 and bps[1]["pulse"] is None, bps
    print("ok")
