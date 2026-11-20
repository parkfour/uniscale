"""
Daily tap counters. Which counters a person has comes from their profile (`counter`: a title and a
list of element labels, e.g. "D Tracker" with "D(out)" and "D(in)", or a formula such as "D(out)+D(in)=D"
whose result is computed each day, never tapped); counts are kept per day in counters.json next to the
vault, keyed by element_key(label), and each tap is also logged to the vault.
"""
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from uniscale.config import CounterSet, element_key, evaluate_formula, load_profile, vault_db_path

logger = logging.getLogger(__name__)


def _counter_file(db_path: Optional[str] = None) -> str:
    base = os.path.dirname(os.path.abspath(db_path or vault_db_path()))
    return os.path.join(base, "counters.json")


def _spec(db_path: Optional[str]) -> Optional[CounterSet]:
    return load_profile(db_path or vault_db_path()).counter


def _elements(spec: Optional[CounterSet]) -> List[Dict[str, str]]:
    return [{"key": element_key(label), "label": label} for label in (spec.elements if spec else [])]


def _resolve(item: str, spec: Optional[CounterSet]) -> str:
    """The element key for `item`, given as a key or a label."""
    for e in _elements(spec):
        if item in (e["key"], e["label"]) or element_key(item) == e["key"]:
            return e["key"]
    known = ", ".join(e["label"] for e in _elements(spec)) or "none: set a counter in your profile"
    raise ValueError(f"Unknown counter '{item}' (counters: {known})")


def _today(target_date: Optional[str]) -> str:
    return target_date or datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")


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
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _counts(entry: Dict[str, Any], keys: List[str]) -> Dict[str, int]:
    return {k: max(0, int(entry.get(k, 0) or 0)) for k in keys}


def get_daily_history(db_path: Optional[str] = None, days: int = 30, spec: Optional[CounterSet] = None) -> List[Dict[str, Any]]:
    """
    Per-day counts for the profile's counters, newest first: [{date, counts: {key: n}, total}], plus
    `computed` (the formula's result that day) when the counter set has a formula.
    """
    spec = spec or _spec(db_path)
    keys = [e["key"] for e in _elements(spec)]
    computed = spec.computed if spec else None
    history = []
    for d_str, entry in _load_counters_raw(db_path)["by_date"].items():
        counts = _counts(entry, keys)
        row = {"date": d_str, "counts": counts, "total": sum(counts.values()), "updated_at": entry.get("updated_at")}
        if computed:
            row["computed"] = evaluate_formula(computed["terms"], counts)
        history.append(row)
    history.sort(key=lambda x: x["date"], reverse=True)
    return history[:days] if days and days > 0 else history


def get_counter_state(db_path: Optional[str] = None, target_date: Optional[str] = None, spec: Optional[CounterSet] = None) -> Dict[str, Any]:
    """
    The counter set (title and elements, empty when the profile has none), the counts for
    target_date (default today), all-time totals, recent taps and the last 30 days. With a formula,
    `computed` holds its result for the day ({key, label, value, total}: total = sum over all days).
    """
    spec = spec or _spec(db_path)
    elements = _elements(spec)
    keys = [e["key"] for e in elements]
    day = _today(target_date)
    raw = _load_counters_raw(db_path)
    by_date = raw["by_date"]
    counts = _counts(by_date.get(day, {}), keys)
    computed = None
    if spec and spec.computed:
        c = spec.computed
        daily = [evaluate_formula(c["terms"], _counts(d, keys)) for d in by_date.values()]
        computed = {"key": c["key"], "label": c["label"], "value": evaluate_formula(c["terms"], counts),
                    "total": round(sum(v for v in daily if v is not None), 2)}
    return {
        "title": spec.title if spec else None,
        "elements": elements,
        "formula": spec.formula if spec else None,
        "computed": computed,
        "date": day,
        "counts": counts,
        "totals": {k: sum(_counts(d, [k])[k] for d in by_date.values()) for k in keys},
        "history": raw["history"][-20:],
        "daily_history": get_daily_history(db_path, days=30, spec=spec),
        "updated_at": by_date.get(day, {}).get("updated_at"),
    }


def adjust_counter(
    item: str,
    delta: int,
    db_path: Optional[str] = None,
    target_date: Optional[str] = None,
    timestamp: Optional[str] = None
) -> Dict[str, Any]:
    """Adds delta (never below 0) to one counter for the day; logged to counters.json and the vault."""
    spec = _spec(db_path)
    key = _resolve(item, spec)
    label = next(e["label"] for e in _elements(spec) if e["key"] == key)
    ts = timestamp or datetime.now(timezone.utc).isoformat()
    day = _today(target_date)

    raw = _load_counters_raw(db_path)
    day_entry = raw["by_date"].setdefault(day, {})
    new_val = max(0, int(day_entry.get(key, 0) or 0) + delta)
    day_entry[key] = new_val
    day_entry["updated_at"] = ts
    raw["history"].append({"timestamp": ts, "date": day, "item": key, "display_name": label, "delta": delta, "new_value": new_val})
    raw["history"] = raw["history"][-500:]
    _save_counters_raw(raw, db_path)

    v_path = db_path or vault_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(v_path)), exist_ok=True)
    record = {
        "type": "counter", "date": day, "timestamp": ts, "title": spec.title if spec else None,
        "item": key, "display_name": label, "delta": delta, "new_value": new_val,
    }
    with open(v_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")

    return get_counter_state(db_path, day, spec)


def set_counter(counts: Dict[str, int], db_path: Optional[str] = None, target_date: Optional[str] = None,
                spec: Optional[CounterSet] = None) -> Dict[str, Any]:
    """Sets explicit counts for the day; `counts` is keyed by element key or label."""
    spec = spec or _spec(db_path)
    resolved = {_resolve(item, spec): max(0, int(n)) for item, n in counts.items()}
    day = _today(target_date)
    raw = _load_counters_raw(db_path)
    day_entry = raw["by_date"].setdefault(day, {})
    day_entry.update(resolved)
    day_entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_counters_raw(raw, db_path)
    return get_counter_state(db_path, day, spec)


def import_historic_records(entries: List[Dict[str, Any]], db_path: Optional[str] = None) -> int:
    """
    Sets whole days from [{"date": "YYYY-MM-DD", "counts": {"D(out)": 3, ...}}, ...]; the counts may
    also sit directly on the entry ({"date": ..., "d_out": 3}). Other fields are ignored.
    """
    spec = _spec(db_path)
    keys = {e["key"] for e in _elements(spec)}
    count = 0
    for e in entries:
        day = str(e.get("date", "")).strip()[:10]
        if not day:
            continue
        given = e.get("counts") if isinstance(e.get("counts"), dict) else e
        counts = {}
        for item, n in given.items():
            if item != "date" and (item in keys or element_key(str(item)) in keys):
                counts[item] = n
        set_counter(counts, db_path=db_path, target_date=day, spec=spec)
        count += 1
    return count
