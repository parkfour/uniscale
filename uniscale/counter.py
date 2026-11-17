import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uniscale.config import vault_db_path

logger = logging.getLogger(__name__)


def _counter_file(db_path: Optional[str] = None) -> str:
    base = os.path.dirname(os.path.abspath(db_path or vault_db_path()))
    return os.path.join(base, "counters.json")


def _normalize_item(item: str) -> str:
    cleaned = (item or "").strip().lower()
    if cleaned in ("d_out", "dout", "d(out)", "out"):
        return "d_out"
    if cleaned in ("d_in", "din", "d(in)", "in"):
        return "d_in"
    return cleaned


def _load_counters_raw(db_path: Optional[str] = None) -> Dict[str, Any]:
    path = _counter_file(db_path)
    if not os.path.exists(path):
        return {"by_date": {}, "history": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"by_date": {}, "history": []}
            data.setdefault("by_date", {})
            data.setdefault("history", [])
            return data
    except Exception as e:
        logger.warning(f"Error loading counters from {path}: {e}")
        return {"by_date": {}, "history": []}


def _save_counters_raw(data: Dict[str, Any], db_path: Optional[str] = None) -> None:
    path = _counter_file(db_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_daily_history(db_path: Optional[str] = None, days: int = 30) -> List[Dict[str, Any]]:
    """Returns sorted daily history of D(out) and D(in) counts, newest first."""
    raw = _load_counters_raw(db_path)
    by_date = raw.get("by_date", {})
    history = []
    for d_str, counts in by_date.items():
        history.append({
            "date": d_str,
            "d_out": int(counts.get("d_out", 0) or 0),
            "d_in": int(counts.get("d_in", 0) or 0),
            "updated_at": counts.get("updated_at")
        })
    history.sort(key=lambda x: x["date"], reverse=True)
    if days is not None and days > 0:
        return history[:days]
    return history


def get_counter_state(db_path: Optional[str] = None, target_date: Optional[str] = None) -> Dict[str, Any]:
    """
    Returns the current counter state for target_date (default today YYYY-MM-DD),
    including D(out) and D(in) counts, all-time totals, recent event history,
    and daily history breakdown.
    """
    today_str = target_date or datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    raw = _load_counters_raw(db_path)
    by_date = raw.get("by_date", {})
    entry = by_date.get(today_str, {"d_out": 0, "d_in": 0})

    d_out = int(entry.get("d_out", 0) or 0)
    d_in = int(entry.get("d_in", 0) or 0)

    # Calculate all-time totals
    all_time_out = sum(int(d.get("d_out", 0) or 0) for d in by_date.values())
    all_time_in = sum(int(d.get("d_in", 0) or 0) for d in by_date.values())

    return {
        "date": today_str,
        "d_out": max(0, d_out),
        "d_in": max(0, d_in),
        "total_d_out": max(0, all_time_out),
        "total_d_in": max(0, all_time_in),
        "history": raw.get("history", [])[-20:],
        "daily_history": get_daily_history(db_path, days=30),
        "updated_at": entry.get("updated_at")
    }


def adjust_counter(
    item: str,
    delta: int,
    db_path: Optional[str] = None,
    target_date: Optional[str] = None,
    timestamp: Optional[str] = None
) -> Dict[str, Any]:
    """
    Increments or decrements D(out) or D(in) by delta.
    Persists to counters.json and appends an audit event to measurements.jsonl.
    """
    normalized_item = _normalize_item(item)
    if normalized_item not in ("d_out", "d_in"):
        raise ValueError(f"Unknown counter item '{item}'. Must be 'd_out' or 'd_in'.")

    now = datetime.now(timezone.utc)
    ts = timestamp or now.isoformat()
    today_str = target_date or now.astimezone().strftime("%Y-%m-%d")

    raw = _load_counters_raw(db_path)
    by_date = raw.setdefault("by_date", {})
    day_entry = by_date.setdefault(today_str, {"d_out": 0, "d_in": 0})

    cur_val = int(day_entry.get(normalized_item, 0) or 0)
    new_val = max(0, cur_val + delta)
    day_entry[normalized_item] = new_val
    day_entry["updated_at"] = ts

    # Audit history log in counters.json
    raw.setdefault("history", []).append({
        "timestamp": ts,
        "date": today_str,
        "item": normalized_item,
        "display_name": "D(out)" if normalized_item == "d_out" else "D(in)",
        "delta": delta,
        "new_value": new_val
    })
    # Keep history bounded to 500 items
    if len(raw["history"]) > 500:
        raw["history"] = raw["history"][-500:]

    _save_counters_raw(raw, db_path)

    # Append to measurements.jsonl
    v_path = db_path or vault_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(v_path)), exist_ok=True)
    measurement_record = {
        "type": "counter",
        "date": today_str,
        "timestamp": ts,
        "item": normalized_item,
        "display_name": "D(out)" if normalized_item == "d_out" else "D(in)",
        "delta": delta,
        "d_out": day_entry.get("d_out", 0),
        "d_in": day_entry.get("d_in", 0)
    }
    with open(v_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(measurement_record) + "\n")

    return get_counter_state(db_path, today_str)


def set_counter(
    d_out: Optional[int] = None,
    d_in: Optional[int] = None,
    db_path: Optional[str] = None,
    target_date: Optional[str] = None
) -> Dict[str, Any]:
    """Sets explicit counter values for the target date."""
    now = datetime.now(timezone.utc)
    today_str = target_date or now.astimezone().strftime("%Y-%m-%d")
    raw = _load_counters_raw(db_path)
    by_date = raw.setdefault("by_date", {})
    day_entry = by_date.setdefault(today_str, {"d_out": 0, "d_in": 0})

    if d_out is not None:
        day_entry["d_out"] = max(0, int(d_out))
    if d_in is not None:
        day_entry["d_in"] = max(0, int(d_in))
    day_entry["updated_at"] = now.isoformat()

    _save_counters_raw(raw, db_path)
    return get_counter_state(db_path, today_str)


def seed_historic_data(days: int = 30, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Seeds realistic historical daily counts for the last `days` days (from `days` ago up to yesterday).
    Does not overwrite existing days in counters.json.
    """
    from datetime import timedelta
    import random
    rnd = random.Random(42)

    today = datetime.now(timezone.utc).astimezone().date()
    raw = _load_counters_raw(db_path)
    by_date = raw.setdefault("by_date", {})
    history = raw.setdefault("history", [])

    v_path = db_path or vault_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(v_path)), exist_ok=True)

    added = []
    for i in range(days, 0, -1):
        d = today - timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")
        if d_str in by_date:
            continue

        d_out = rnd.randint(2, 6)
        d_in = rnd.randint(1, 4)
        ts = datetime(d.year, d.month, d.day, 21, 30, 0, tzinfo=timezone.utc).isoformat()

        by_date[d_str] = {
            "d_out": d_out,
            "d_in": d_in,
            "updated_at": ts
        }

        history.append({
            "timestamp": ts,
            "date": d_str,
            "item": "d_out",
            "display_name": "D(out)",
            "delta": d_out,
            "new_value": d_out
        })
        history.append({
            "timestamp": ts,
            "date": d_str,
            "item": "d_in",
            "display_name": "D(in)",
            "delta": d_in,
            "new_value": d_in
        })

        rec = {
            "type": "counter",
            "date": d_str,
            "timestamp": ts,
            "item": "d_out",
            "display_name": "D(out)",
            "delta": d_out,
            "d_out": d_out,
            "d_in": d_in
        }
        with open(v_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

        added.append({"date": d_str, "d_out": d_out, "d_in": d_in})

    _save_counters_raw(raw, db_path)
    return added


def import_historic_records(entries: List[Dict[str, Any]], db_path: Optional[str] = None) -> int:
    """
    Imports a list of historical daily counts:
    [{"date": "YYYY-MM-DD", "d_out": int, "d_in": int}, ...]
    """
    raw = _load_counters_raw(db_path)
    by_date = raw.setdefault("by_date", {})
    v_path = db_path or vault_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(v_path)), exist_ok=True)

    count = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for e in entries:
        d_str = str(e.get("date", "")).strip()[:10]
        if not d_str:
            continue
        d_out = max(0, int(e.get("d_out", 0) or 0))
        d_in = max(0, int(e.get("d_in", 0) or 0))

        by_date[d_str] = {
            "d_out": d_out,
            "d_in": d_in,
            "updated_at": now_iso
        }

        rec = {
            "type": "counter",
            "date": d_str,
            "timestamp": now_iso,
            "item": "d_out",
            "display_name": "D(out)",
            "delta": d_out,
            "d_out": d_out,
            "d_in": d_in
        }
        with open(v_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        count += 1

    _save_counters_raw(raw, db_path)
    return count

