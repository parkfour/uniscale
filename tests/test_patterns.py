import json
import os
from datetime import date, timedelta

from uniscale import ai
from uniscale.counter import set_counter
from uniscale.patterns import build_patterns, patterns_prompt
from uniscale.readings import load_vault

TODAY = date(2026, 11, 18)


def _vault(tmp_path, days=20):
    """D(out) taps of 0-2 a day; next-morning systolic goes up 6 per tap. Brewery visits every 4th day."""
    vault = str(tmp_path / "measurements.jsonl")
    with open(tmp_path / "profile.json", "w") as f:
        json.dump({"counter": {"title": "D Tracker", "elements": ["D(out)", "D(in)"]}}, f)
    records, stays = [], []
    for i in range(days):
        d = TODAY - timedelta(days=days - 1 - i)
        taps = i % 3
        set_counter({"D(out)": taps}, db_path=vault, target_date=d.isoformat())
        nxt = d + timedelta(days=1)
        records.append({"type": "blood_pressure", "timestamp": f"{nxt}T07:00:00", "systolic": 120 + 6 * taps,
                        "diastolic": 80, "pulse": 60})
        records.append({"type": "activity", "date": d.isoformat(), "steps": 5000 + 10 * i, "resting_heart_rate": 58})
        stays.append({"id": f"h{i}", "date": d.isoformat(), "avg_lat": 30.0, "avg_lon": -86.0, "name": "Home",
                      "duration_seconds": 50000})
        if i % 4 == 0:
            stays.append({"id": f"b{i}", "date": d.isoformat(), "avg_lat": 30.3, "avg_lon": -86.3,
                          "name": "Between Brewery", "duration_seconds": 7200})
    with open(vault, "w") as f:
        f.writelines(json.dumps(r) + "\n" for r in records)
    with open(tmp_path / "places.json", "w") as f:
        json.dump({"places": stays, "known": []}, f)
    return vault


def test_daily_log_and_next_day_pattern(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    monkeypatch.setattr("uniscale.patterns.date", type("D", (date,), {"today": staticmethod(lambda: TODAY)}))
    data = build_patterns(vault, load_vault(vault))

    first = data["rows"][-1]
    assert first["date"] == TODAY.isoformat() and first["c:d_out"] == 19 % 3 and first["c:d_in"] == 0
    brewery_day = next(r for r in data["rows"] if r.get("places"))
    assert brewery_day["places"] == ["Between Brewery"] and brewery_day["away_h"] == 2.0  # Home isn't "away"

    top = data["patterns"][0]
    assert (top["driver"], top["metric"], top["when"]) == ("D(out)", "systolic BP", "next day")
    assert top["r"] == 1.0 and top["days"] >= 8
    assert not any(p["driver"] == "D(in)" for p in data["patterns"])  # never changes, so no correlation

    text = patterns_prompt(data)
    assert "D(out) vs next day systolic BP: r=+1.00" in text
    assert "date,D(out),D(in),away_h,places" in text and "Between Brewery" in text
    assert 'D(out), D(in) are how many times a day the person tapped each counter of their "D Tracker"' in text


def test_log_days_trims_the_table_but_not_the_patterns(tmp_path, monkeypatch):
    vault = _vault(tmp_path, days=40)
    monkeypatch.setattr("uniscale.patterns.date", type("D", (date,), {"today": staticmethod(lambda: TODAY)}))
    data = build_patterns(vault, load_vault(vault))
    short = patterns_prompt(data, log_days=10)
    assert (TODAY - timedelta(days=9)).isoformat() in short and (TODAY - timedelta(days=10)).isoformat() not in short
    assert "r=+1.00" in short


def test_no_counter_or_places_still_builds(tmp_path):
    vault = str(tmp_path / "measurements.jsonl")
    open(vault, "w").close()
    data = build_patterns(vault, [])
    assert data == {"title": None, "counters": {}, "rows": [], "patterns": []}
    assert patterns_prompt(data) == ""


def test_dossier_carries_the_patterns_and_context_follows_config(tmp_path, monkeypatch):
    vault = _vault(tmp_path)
    monkeypatch.setattr("uniscale.patterns.date", type("D", (date,), {"today": staticmethod(lambda: TODAY)}))
    sent = {}

    def fake_query(chat, **kw):
        sent["system"] = chat[0]["content"]
        return "ok", False
    monkeypatch.setattr(ai, "query_chat", fake_query)

    ai.chat_with_agent([{"role": "user", "content": "hi"}], db_path=vault, context_tokens=32768)
    assert "Daily Log (last 90 days" in sent["system"] and "D(out) vs next day systolic BP" in sent["system"]
    ai.chat_with_agent([{"role": "user", "content": "hi"}], db_path=vault)
    assert "Daily Log (last 30 days" in sent["system"]

    big = [{"role": "system", "content": "s"}] + [{"role": r, "content": "x" * 3000}
                                                    for r in ("user", "assistant") * 20]
    assert len(ai._fit_context(big, 32768)) > len(ai._fit_context(big))
