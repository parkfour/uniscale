import json
from datetime import date, timedelta

from fastapi.testclient import TestClient

from uniscale.heart_rate import KEEP_DAYS, add_samples, recent
from uniscale.server import app


def test_heart_rate_store_merges_and_keeps_a_week(tmp_path):
    vault = str(tmp_path / "measurements.jsonl")
    now = 1_800_000_000
    add_samples([[now - 120, 70], [now - 60, 72]], db_path=vault, now=now)
    # Re-sent samples overwrite themselves; very old ones are dropped
    kept = add_samples([[now - 60, 75], [now, 80], [now - (KEEP_DAYS + 1) * 86400, 60]], db_path=vault, now=now)
    assert kept == 3
    r = recent(hours=2, db_path=vault)
    assert r["end"] == now and r["samples"] == [[now - 120, 70], [now - 60, 75], [now, 80]]
    # The window ends at the newest sample, not at the clock
    assert recent(hours=1 / 60, db_path=vault)["samples"] == [[now - 60, 75], [now, 80]]
    assert recent(db_path=str(tmp_path / "other" / "m.jsonl")) == {"samples": [], "end": None}


def test_heart_rate_and_activity_days_api(tmp_path, monkeypatch):
    vault = tmp_path / "measurements.jsonl"
    today = date.today()
    days = [(today - timedelta(days=i)).isoformat() for i in range(3)]
    vault.write_text("\n".join(json.dumps(r) for r in [
        {"type": "activity", "date": days[2], "steps": 9000, "distance_km": 7.1, "resting_heart_rate": 61},
        {"type": "activity", "date": days[1], "steps": 8000, "distance_km": 6.4, "active_minutes": 35},
        {"type": "activity", "date": days[0], "steps": 1200, "distance_km": 1.0},
    ]) + "\n")
    monkeypatch.setenv("UNISCALE_DB_PATH", str(vault))
    client = TestClient(app)

    assert client.post("/api/sync/heart_rate", json={"samples": [[1_800_000_000, 70], [1_800_000_060, 300]]}).status_code == 201
    assert client.post("/api/sync/heart_rate", json={"samples": [[1, 2, 3]]}).status_code == 422
    hr = client.get("/api/heart_rate", params={"hours": 2}).json()
    assert hr["samples"] == [[1_800_000_000, 70]]  # 300 bpm is dropped

    listed = client.get("/api/activity/days").json()["days"]
    assert [d["date"] for d in listed] == [days[1], days[2]]  # yesterday first; today isn't complete
    assert client.get("/api/summary").json()["latest_resting_hr"] == {"bpm": 61, "date": days[2]}

    page = client.get("/").text
    assert 'id="history-table" style="display: none;"' in page and "toggleHistory" in page
    assert "Sync Pipeline" not in page and "Zero Garmin Cloud" not in page
    # Cards in rows: sleep, ECG, supplements / counter, places, patterns / labs, genomics, bio age
    order = [page.index(m) for m in ("<!-- Sleep Architecture", "<!-- Lead-I ECG", "<!-- Daily Protocol",
                                     "<!-- Daily tap counter", "<!-- Dwell Places", "<!-- Patterns:",
                                     "<!-- Clinical Labs", "<!-- Sovereign Genomics", "<!-- Biological Age")]
    assert order == sorted(order)
