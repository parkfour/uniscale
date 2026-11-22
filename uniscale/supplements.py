import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from uniscale.models import SupplementIntake
from uniscale.config import vault_db_path

logger = logging.getLogger(__name__)

DEFAULT_SUGGESTION_CATALOG: List[Dict[str, Any]] = [
    {
        "name": "Vitamin D3 + K2",
        "dosage": 5000.0,
        "unit": "IU",
        "category": "vitamin",
        "frequency": "morning",
        "purpose": "Bone density, arterial calcification prevention & immune modulation"
    },
    {
        "name": "Omega-3 Fish Oil (EPA/DHA)",
        "dosage": 2000.0,
        "unit": "mg",
        "category": "supplement",
        "frequency": "morning",
        "purpose": "Cardiovascular resilience, lipid optimization & systemic anti-inflammatory"
    },
    {
        "name": "Magnesium Glycinate",
        "dosage": 200.0,
        "unit": "mg",
        "category": "mineral",
        "frequency": "evening",
        "purpose": "Restorative slow-wave sleep, muscle relaxation & neuromuscular support"
    },
    {
        "name": "Creatine Monohydrate",
        "dosage": 5000.0,
        "unit": "mg",
        "category": "supplement",
        "frequency": "daily",
        "purpose": "Cellular ATP phosphocreatine reserves, strength & neuroprotection"
    },
    {
        "name": "CoQ10 (Ubiquinol)",
        "dosage": 100.0,
        "unit": "mg",
        "category": "supplement",
        "frequency": "morning",
        "purpose": "Mitochondrial bioenergetics & cardiovascular endothelial health"
    },
    {
        "name": "Zinc Picolinate",
        "dosage": 15.0,
        "unit": "mg",
        "category": "mineral",
        "frequency": "evening",
        "purpose": "Immune defense, enzymatic synthesis & hormone optimization"
    },
    {
        "name": "NMN (Nicotinamide Mononucleotide)",
        "dosage": 500.0,
        "unit": "mg",
        "category": "longevity",
        "frequency": "morning",
        "purpose": "NAD+ salvage pathway, sirtuin activation & metabolic cellular repair"
    },
    {
        "name": "Ashwagandha (KSM-66)",
        "dosage": 300.0,
        "unit": "mg",
        "category": "supplement",
        "frequency": "evening",
        "purpose": "Adrenal cortisol regulation, stress resilience & autonomic balance"
    }
]


# When in the day a dose is taken, in day order. A stack item lists the slots it's taken in.
SLOTS = ["wake", "breakfast", "lunch", "dinner", "bed"]


def _default_slots(frequency: str) -> List[str]:
    """Slots for a stack item saved before slots existed, from its frequency."""
    f = frequency.lower()
    if "need" in f or "workout" in f:
        return []
    if "meal" in f:
        return ["breakfast", "lunch", "dinner"]
    if "bed" in f:
        return ["bed"]
    if "even" in f or "night" in f or "pm" in f:
        return ["dinner"]
    return ["breakfast"]


def get_stack_file_path(db_path: Optional[str] = None) -> str:
    # Lives next to the measurements vault, so the CLI and web UI share one stack file.
    return os.path.join(os.path.dirname(os.path.abspath(db_path or vault_db_path())), "supplements_stack.json")


def _normalize_stack_item(item: Dict[str, Any]) -> Dict[str, Any]:
    try:
        dosage_val = float(item.get("dosage", 0.0) or 0.0)
    except (ValueError, TypeError):
        dosage_val = 0.0
    frequency = str(item.get("frequency", "daily")).strip() or "daily"
    raw_slots = item.get("slots")
    # An explicit empty list means "as needed": not part of the daily checklist schedule.
    slots = [s for s in SLOTS if s in raw_slots] if isinstance(raw_slots, list) else _default_slots(frequency)
    return {
        "name": str(item.get("name", "")).strip(),
        "dosage": dosage_val,
        "unit": str(item.get("unit", "mg")).strip() or "mg",
        "category": str(item.get("category", "supplement")).strip() or "supplement",
        "frequency": frequency,
        "slots": slots,
        "purpose": str(item.get("purpose", "")).strip()
    }


def active_stack(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Returns the active daily regimen. Priority:
    1. Persistent user-edited stack in supplements_stack.json
    2. Config file [[supplements.stack]]
    3. Empty list if none configured
    """
    stack_file = get_stack_file_path(db_path)
    if os.path.exists(stack_file):
        try:
            with open(stack_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return [_normalize_stack_item(item) for item in data if isinstance(item, dict) and item.get("name")]
        except Exception as e:
            logger.warning(f"Error reading {stack_file}: {e}")

    # Fallback to config file
    from uniscale.config import load_config
    cfg = load_config()
    cfg_stack = [item.model_dump() for item in cfg.supplements.stack]
    return [_normalize_stack_item(item) for item in cfg_stack]


def save_active_stack(stack: List[Dict[str, Any]], db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Persists the user's active regimen stack to supplements_stack.json.
    """
    path = get_stack_file_path(db_path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    cleaned = [_normalize_stack_item(item) for item in stack if str(item.get("name", "")).strip()]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cleaned, f, indent=2)
    logger.info(f"Saved {len(cleaned)} supplements to active stack in {path}")
    return cleaned


def add_to_stack(item: Dict[str, Any], db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Adds or updates a supplement compound in the active stack.
    """
    normalized = _normalize_stack_item(item)
    if not normalized["name"]:
        raise ValueError("Supplement name cannot be blank")

    key = normalized["name"].lower()
    current = [s for s in active_stack(db_path) if s["name"].lower() != key]
    return save_active_stack(current + [normalized], db_path)


def remove_from_stack(name: str, db_path: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """
    Removes a supplement compound from the active stack.
    Returns None (and writes nothing) if no compound by that name exists.
    """
    current = active_stack(db_path)
    target = name.strip().lower()
    filtered = [item for item in current if item["name"].strip().lower() != target]
    if len(filtered) == len(current):
        return None
    return save_active_stack(filtered, db_path)


def reset_stack_to_config(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Deletes the persistent stack file and reverts back to config.toml.
    """
    path = get_stack_file_path(db_path)
    if os.path.exists(path):
        os.remove(path)
    return active_stack(db_path)


def delete_supplement_intake(timestamp_str: str, name: Optional[str] = None, db_path: Optional[str] = None) -> bool:
    """
    Deletes a logged supplement intake event from measurements.jsonl by timestamp and optional name.
    Only records of type "supplement" are eligible; the file is replaced atomically.
    """
    path = db_path or vault_db_path()
    if not os.path.exists(path):
        return False
    target_name = name.strip().lower() if name else None
    kept = []
    removed = False
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line_str = line.strip()
            if not line_str:
                continue
            if not removed:
                try:
                    d = json.loads(line_str)
                except ValueError:
                    d = None
                if (
                    isinstance(d, dict)
                    and d.get("type") == "supplement"
                    and str(d.get("timestamp", "")) == timestamp_str
                    and (target_name is None or str(d.get("name") or "").strip().lower() == target_name)
                ):
                    removed = True
                    continue
            kept.append(line_str)
    if removed:
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.writelines(l + "\n" for l in kept)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    return removed


def log_supplement_intake(
    name: str,
    dosage: float,
    unit: str = "mg",
    category: str = "supplement",
    frequency: str = "daily",
    notes: Optional[str] = None,
    db_path: Optional[str] = None,
    timestamp: Optional[datetime] = None,
    slot: Optional[str] = None
) -> Dict[str, Any]:
    """
    Appends a new supplement/medication intake event to measurements.jsonl.
    A client-supplied timestamp makes the call idempotent: the phone resends queued check-offs
    with the time they were tapped, and a record with the same name and time isn't written twice.
    """
    ts = timestamp or datetime.now(timezone.utc)
    intake = SupplementIntake(
        name=name.strip(),
        dosage=dosage,
        unit=unit.strip(),
        category=category.strip(),
        frequency=frequency.strip(),
        timestamp=ts,
        notes=notes,
        slot=slot if slot in SLOTS else None,
        type="supplement"
    )

    path = db_path or vault_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    data = intake.model_dump()
    data["timestamp"] = ts.isoformat()
    if data["slot"] is None:
        del data["slot"]

    if timestamp is not None and os.path.exists(path):
        key = intake.name.lower()
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if '"supplement"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if (d.get("type") == "supplement" and str(d.get("timestamp")) == data["timestamp"]
                        and str(d.get("name") or "").strip().lower() == key):
                    return d

    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(data) + "\n")

    logger.info(f"Logged supplement intake: {intake.name} {intake.dosage}{intake.unit}")
    return data


def get_supplements_summary(
    db_path: Optional[str] = None,
    days: int = 7
) -> Dict[str, Any]:
    """
    Retrieves supplement intake records, adherence stats, and today's status.
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
                    if d.get("type") == "supplement":
                        records.append(d)
                except Exception:
                    pass

    from uniscale.day import local_day, today, zone
    tz = zone(path)
    now = today(path)

    # Sort chronological
    records.sort(key=lambda x: str(x.get("timestamp") or ""))

    today_str = now.isoformat()

    # Group by date
    today_records: List[Dict[str, Any]] = []
    recent_history: List[Dict[str, Any]] = []
    cutoff_date = (now - timedelta(days=days)).isoformat()

    # Timestamps are stored in UTC; bucket them by the person's calendar day.
    for r in records:
        r["_day"] = ts = local_day(r.get("timestamp"), tz)
        if ts >= cutoff_date:
            recent_history.append(r)
        if ts == today_str:
            today_records.append(r)

    # One checklist entry per stack item per slot (an item with no slots appears once, slot None),
    # in day order. A logged intake ticks the slot it names; one without a slot (logged before
    # slots existed, or from the CLI) ticks that item's earliest open slot.
    checklist = []
    stack = active_stack(db_path)
    for item in stack:
        norm_name = item["name"].lower()
        recs = [r for r in today_records
                if (k := str(r.get("name") or "").strip().lower()) and (k in norm_name or norm_name in k)]
        slots = item.get("slots") or [None]
        by_slot = {r.get("slot"): r for r in recs if r.get("slot") in slots}
        loose = [r for r in recs if r.get("slot") not in slots or r.get("slot") is None]
        for slot in slots:
            matched_record = by_slot.get(slot) or (loose.pop(0) if loose else None)
            checklist.append({
                "name": item["name"],
                "dosage": item["dosage"],
                "unit": item["unit"],
                "category": item["category"],
                "frequency": item["frequency"],
                "slot": slot,
                "purpose": item.get("purpose", ""),
                "taken_today": matched_record is not None,
                "taken_at": matched_record.get("timestamp") if matched_record else None
            })
    checklist.sort(key=lambda c: SLOTS.index(c["slot"]) if c["slot"] in SLOTS else len(SLOTS))

    # Adherence over last N days
    adherence_by_day: Dict[str, int] = {}
    for i in range(days):
        day_date = (now - timedelta(days=i)).isoformat()
        count = sum(1 for r in recent_history if r["_day"] == day_date)
        adherence_by_day[day_date] = count

    taken_count = sum(1 for c in checklist if c["taken_today"])
    total_regimen = len(checklist)

    return {
        "today_date": today_str,
        "completed_count": taken_count,
        "total_count": total_regimen,
        "completion_rate_percent": round((taken_count / total_regimen) * 100.0, 1) if total_regimen > 0 else 0,
        "checklist": checklist,
        "recent_intakes": list(reversed(records[-20:])),
        "adherence_by_day": adherence_by_day,
        "active_stack": stack,
        "suggestion_catalog": DEFAULT_SUGGESTION_CATALOG
    }
