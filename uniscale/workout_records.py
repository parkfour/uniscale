"""
Personal records per sport, for badges on the workouts: the fastest lap and the best average speed,
over the last 30 days and of all time. Laps are the watch's splits (auto-laps, usually 1 km); only full
laps count, so a short last lap can't set a record.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

MONTH_DAYS = 30
MIN_RECORD_M = 1000   # shorter workouts don't compete for best average speed
MIN_LAP_M = 400       # nor laps shorter than this
FULL_LAP = 0.9        # a lap counts when it's at least 90% of the workout's longest lap
# Faster than this isn't the sport (a drive saved as a walk, a GPS jump): no record. km/h.
MAX_KMH = {"walking": 12, "hiking": 12, "running": 25, "treadmill_running": 25, "trail_running": 25}
DEFAULT_MAX_KMH = 100

BADGES = {
    ("fastest_lap", "ever"): "Fastest lap ever",
    ("fastest_lap", "month"): "Fastest lap this month",
    ("best_speed", "ever"): "Best avg speed ever",
    ("best_speed", "month"): "Best avg speed this month",
}


def _when(w: Dict[str, Any]) -> Optional[datetime]:
    try:
        t = datetime.fromisoformat(str(w.get("timestamp")).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _distance_m(w: Dict[str, Any]) -> float:
    return float(w.get("distance_meters") or (w.get("distance_km") or 0) * 1000)


def _avg_speed(w: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    d, t = _distance_m(w), float(w.get("duration_seconds") or 0)
    if d < MIN_RECORD_M or t <= 0:
        return None
    return {"speed_mps": d / t, "distance_m": round(d), "seconds": round(t)}


def _fastest_lap(w: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    laps = [s for s in (w.get("splits") or []) if s.get("distance_m") and s.get("seconds")]
    if not laps:
        return None
    longest = max(s["distance_m"] for s in laps)
    best = None
    for n, s in enumerate(laps, 1):
        if s["distance_m"] < max(MIN_LAP_M, FULL_LAP * longest):
            continue
        speed = s["distance_m"] / s["seconds"]
        if best is None or speed > best["speed_mps"]:
            best = {"speed_mps": speed, "distance_m": round(s["distance_m"]), "seconds": round(s["seconds"], 1), "lap": n}
    return best


def workout_records(workouts: List[Dict[str, Any]], now: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
    """{sport: {"fastest_lap": {"ever": rec, "month": rec}, "best_speed": {...}}}; rec carries the workout's timestamp."""
    since = (now or datetime.now(timezone.utc)) - timedelta(days=MONTH_DAYS)
    out: Dict[str, Dict[str, Any]] = {}
    for w in workouts:
        when = _when(w)
        if when is None:
            continue
        sport = str(w.get("sport") or "generic")
        max_mps = MAX_KMH.get(sport, DEFAULT_MAX_KMH) / 3.6
        for kind, measure in (("fastest_lap", _fastest_lap), ("best_speed", _avg_speed)):
            rec = measure(w)
            if rec is None or rec["speed_mps"] > max_mps:
                continue
            rec = {**rec, "speed_mps": round(rec["speed_mps"], 3), "timestamp": w.get("timestamp")}
            slots = out.setdefault(sport, {}).setdefault(kind, {})
            for span in ("ever", "month"):
                if span == "month" and when < since:
                    continue
                if span not in slots or rec["speed_mps"] > slots[span]["speed_mps"]:
                    slots[span] = rec
    return out


def badges(w: Dict[str, Any], records: Dict[str, Dict[str, Any]]) -> List[str]:
    """The record badges this workout holds; one per kind (all-time beats this month). A lap names its number."""
    mine = records.get(str(w.get("sport") or "generic"), {})
    found = []
    for kind in ("fastest_lap", "best_speed"):
        for span in ("ever", "month"):
            rec = mine.get(kind, {}).get(span)
            if rec and rec["timestamp"] == w.get("timestamp"):
                found.append(BADGES[(kind, span)] + (f" (lap {rec['lap']})" if "lap" in rec else ""))
                break
    return found


def with_badges(workouts: List[Dict[str, Any]], records: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{**w, "badges": badges(w, records)} for w in workouts]
