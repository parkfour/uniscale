"""
Day-by-day log and same-day / next-day correlations between what a person does (their counters, time away
from home, the places they go) and their health metrics, for the chat assistant. The numbers are worked
out here because a small local model can't do the arithmetic reliably; it only gets to explain them.
"""
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from uniscale.day import local_day, today as local_today, zone

DAYS = 90
MIN_DAYS = 8          # fewer paired days than this and a correlation is noise
MIN_R = 0.3           # weaker than this isn't worth the model's attention
MAX_PATTERNS = 15
MIN_PLACE_VISITS = 3  # a place needs this many visit days to be tested on its own
MIN_SIDE = 3          # days needed on each side of the high/low split, so one odd day can't make a pattern

# Metric key -> (label, how to get it from the day's records)
METRICS = {
    "weight_lb": "weight (lb)",
    "systolic": "systolic BP",
    "diastolic": "diastolic BP",
    "resting_hr": "resting HR",
    "sleep_h": "sleep (h)",
    "hrv_ms": "nightly HRV (ms)",
    "sleep_score": "sleep score",
    "steps": "steps",
}


def _mean(xs: List[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 1) if xs else None


def _health_by_day(records: List[Dict[str, Any]], tz) -> Dict[str, Dict[str, float]]:
    """Per day: last weight, mean BP, resting HR and steps, and the night's sleep (its record's date)."""
    per: Dict[str, Dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in records:
        t = r.get("type")
        day = str(r["date"])[:10] if r.get("date") else local_day(r.get("timestamp"), tz)
        if not day:
            continue
        if t == "weight" and (r.get("weight_lb") or r.get("weight_kg")):
            per[day]["weight_lb"].append(r.get("weight_lb") or r["weight_kg"] * 2.20462262)
        elif t == "blood_pressure" and r.get("systolic") and r.get("diastolic"):
            per[day]["systolic"].append(r["systolic"])
            per[day]["diastolic"].append(r["diastolic"])
        elif t == "activity":
            if r.get("resting_heart_rate"):
                per[day]["resting_hr"].append(r["resting_heart_rate"])
            if r.get("steps"):
                per[day]["steps"].append(r["steps"])
        elif t == "sleep" and not r.get("nap"):
            if r.get("duration_seconds"):
                per[day]["sleep_h"].append(r["duration_seconds"] / 3600)
            if r.get("hrv_rmssd_ms"):
                per[day]["hrv_ms"].append(r["hrv_rmssd_ms"])
            if r.get("sleep_score"):
                per[day]["sleep_score"].append(r["sleep_score"])
    out: Dict[str, Dict[str, float]] = {}
    for day, m in per.items():
        out[day] = {k: (round(v[-1], 1) if k == "weight_lb" else _mean(v)) for k, v in m.items()}
    return out


def _places_by_day(db_path: str) -> Dict[str, Dict[str, Any]]:
    """Per day with location data: hours away from home and the named places stayed at (an hour or more)."""
    from uniscale.places import _is_home, _labelled, _load_places_state
    state = _load_places_state(db_path)
    days: Dict[str, Dict[str, Any]] = {}
    for p in state.get("places", []):
        if p.get("is_deleted") or not p.get("date"):
            continue
        p = _labelled(p, state)
        d = days.setdefault(p["date"], {"away_h": 0.0, "places": []})
        name = p.get("name")
        if _is_home(name):
            continue
        d["away_h"] = round(d["away_h"] + p.get("duration_seconds", 0) / 3600, 1)
        label = name or "unnamed place"
        if label not in d["places"]:
            d["places"].append(label)
    return days


def daily_log(db_path: str, records: List[Dict[str, Any]], days: int = DAYS,
              today: Optional[date] = None) -> Dict[str, Any]:
    """
    The last `days` days, oldest first, one row per day that has anything: counter values (0 on a day
    without taps, once counting has started), hours away and places (only on days with location data),
    and the health metrics. Also returns the counter labels, keyed like the rows.
    """
    from uniscale.counter import get_daily_history
    from uniscale.config import element_key, load_profile
    spec = load_profile(db_path).counter
    # "c:" keeps a counter called e.g. "Steps" from overwriting the steps metric in a row
    counters = {"c:" + element_key(label): label for label in (spec.elements if spec else [])}
    computed = spec.computed if spec else None
    if computed:  # the formula's result, e.g. D = D(out) + D(in), is one more column
        counters["c:" + computed["key"]] = computed["label"]
    counted = {h["date"]: {**h["counts"], **({computed["key"]: h["computed"]} if computed else {})}
               for h in get_daily_history(db_path, days=0, spec=spec)} if spec else {}
    first_count = min(counted) if counted else None
    places = _places_by_day(db_path)
    health = _health_by_day(records, zone(db_path))

    end = today or local_today(db_path)
    rows = []
    for i in range(days - 1, -1, -1):
        day = (end - timedelta(days=i)).isoformat()
        row: Dict[str, Any] = {"date": day}
        if first_count and day >= first_count:
            row.update({k: counted.get(day, {}).get(k[2:], 0) for k in counters})
        if day in places:
            row.update(places[day])
        row.update(health.get(day, {}))
        if len(row) > 1:
            rows.append(row)
    return {"title": spec.title if spec else None, "counters": counters, "rows": rows,
            "formula": spec.formula if spec else None}


def _pearson(xs: List[float], ys: List[float]) -> Optional[float]:
    try:
        return statistics.correlation(xs, ys)
    except statistics.StatisticsError:  # too few points, or one side never changes
        return None


def find_patterns(log: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Correlates each driver (a counter, hours away, going to a given place) with each metric the same day
    and the next day. Keeps pairs with at least MIN_DAYS days and |r| >= MIN_R, strongest first, each with
    the metric's average on high vs low driver days so the model can say it in plain numbers.
    """
    rows = {r["date"]: r for r in log["rows"]}
    drivers: Dict[str, str] = {k: label for k, label in log["counters"].items()}
    drivers["away_h"] = "hours away from home"
    visits: Dict[str, int] = defaultdict(int)
    for r in log["rows"]:
        for name in r.get("places", []):
            visits[name] += 1
    place_drivers = [n for n, c in visits.items() if c >= MIN_PLACE_VISITS and n != "unnamed place"]

    def driver_value(row: Dict[str, Any], key: str) -> Optional[float]:
        if key.startswith("place:"):
            return (1.0 if key[6:] in row.get("places", []) else 0.0) if "away_h" in row else None
        return row.get(key)

    for n in place_drivers:
        drivers[f"place:{n}"] = f"going to {n}"

    found = []
    for dkey, dlabel in drivers.items():
        for mkey, mlabel in METRICS.items():
            for lag, when in ((0, "same day"), (1, "next day")):
                xs, ys = [], []
                for day, row in rows.items():
                    x = driver_value(row, dkey)
                    later = rows.get((date.fromisoformat(day) + timedelta(days=lag)).isoformat(), {})
                    y = later.get(mkey)
                    if x is not None and y is not None:
                        xs.append(float(x))
                        ys.append(float(y))
                if len(xs) < MIN_DAYS:
                    continue
                r = _pearson(xs, ys)
                if r is None or abs(r) < MIN_R:
                    continue
                cut = statistics.median(xs)
                high = [y for x, y in zip(xs, ys) if x > cut]
                low = [y for x, y in zip(xs, ys) if x <= cut]
                if min(len(high), len(low)) < MIN_SIDE:
                    continue
                found.append({
                    "driver": dlabel, "metric": mlabel, "when": when, "r": round(r, 2), "days": len(xs),
                    "split": cut, "avg_high": _mean(high), "avg_low": _mean(low),
                })
    found.sort(key=lambda p: abs(p["r"]), reverse=True)
    return found[:MAX_PATTERNS]


def describe(p: Dict[str, Any]) -> str:
    """One plain line for a pattern, e.g. "More D(out) → higher next-day systolic BP (129.7 vs 124.1; r +0.52, 13 days)"."""
    going = p["driver"].startswith("going to")
    who = ("G" + p["driver"][1:]) if going else f"More {p['driver']}"
    way = "higher" if p["avg_high"] > p["avg_low"] else "lower" if p["avg_high"] < p["avg_low"] else "unchanged"
    return (f"{who} → {way} {p['when'].replace(' ', '-')} {p['metric']} "
            f"({p['avg_high']:g} vs {p['avg_low']:g}; r {p['r']:+.2f}, {p['days']} days)")


def build_patterns(db_path: str, records: List[Dict[str, Any]]) -> Dict[str, Any]:
    log = daily_log(db_path, records)
    return {**log, "patterns": find_patterns(log)}


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def patterns_prompt(data: Dict[str, Any], log_days: int = DAYS) -> str:
    """The last `log_days` days of the daily log as a compact table, and the computed patterns."""
    all_rows = data.get("rows") or []
    if not all_rows:
        return ""
    since = (date.fromisoformat(all_rows[-1]["date"]) - timedelta(days=log_days - 1)).isoformat()
    rows = [r for r in all_rows if r["date"] >= since]
    counters = data.get("counters", {})
    cols = list(counters) + ["away_h", "places"] + list(METRICS)
    heads = [counters.get(c, c) for c in cols]
    lines = ["date," + ",".join(heads)]
    for r in rows:
        cells = [";".join(r[c]) if c == "places" and c in r else _fmt(r.get(c)) for c in cols]
        lines.append(r["date"] + "," + ",".join(cells))

    pats = []
    for p in data.get("patterns", []):
        if p["avg_high"] is None or p["avg_low"] is None:
            continue
        hl = ("on days with it", "without") if p["driver"].startswith("going to") else (f"when above {p['split']:g}", "otherwise")
        pats.append(f"   - {p['driver']} vs {p['when']} {p['metric']}: r={p['r']:+.2f} over {p['days']} days "
                    f"({p['metric']} averaged {p['avg_high']:g} {hl[0]}, {p['avg_low']:g} {hl[1]})")
    pat_text = "\n".join(pats) or "   - None strong enough yet (needs |r| >= 0.3 over at least 8 days)."

    formula = data.get("formula")
    tapped = list(counters.values())[:-1] if formula else list(counters.values())
    counter_note = (f"\n   {', '.join(tapped)} are how many times a day the person tapped each counter "
                    f"of their \"{data.get('title')}\" tracker (counts, not hours)." if counters else "")
    if counters and formula:
        counter_note += f" {list(counters.values())[-1]} is not tapped but computed each day: {formula}."
    return f"""8. **Daily Log (last {log_days} days; blank = not measured; away_h and places only on days with location data;
   sleep_h, hrv_ms and sleep_score are the night that ended that morning):**{counter_note}
```
{chr(10).join(lines)}
```

9. **Patterns (computed by the hub, Pearson r; same day and next day):**
{pat_text}
   These are correlations from a few weeks of one person's data, not causes. Explain them in plain terms,
   say how much weight each deserves given its r and number of days, and suggest what to track to test them.
   Use these figures; don't compute your own statistics from the log."""
