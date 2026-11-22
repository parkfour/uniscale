import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from uniscale.day import local_day, set_zone, today, zone, zone_name
from uniscale.patterns import _health_by_day
from uniscale.server import app

CHICAGO = ZoneInfo("America/Chicago")


def test_local_day_follows_the_zone():
    # 03:30 UTC on the 22nd is still the evening of the 21st in Chicago
    assert local_day("2026-11-22T03:30:00+00:00", CHICAGO) == "2026-11-21"
    assert local_day("2026-11-22T03:30:00Z", CHICAGO) == "2026-11-21"
    assert local_day("2026-11-22T15:00:00+00:00", CHICAGO) == "2026-11-22"
    assert local_day("2026-11-22T03:30:00", CHICAGO) == "2026-11-22"  # no offset: already local
    assert local_day("2026-11-22", CHICAGO) == "2026-11-22"
    assert local_day("garbage", CHICAGO) == "garbage"[:10]


def test_zone_defaults_to_the_hub_then_follows_the_wearable(tmp_path):
    vault = str(tmp_path / "measurements.jsonl")
    assert zone_name(vault) is None
    assert zone(vault).utcoffset(datetime.now()) == datetime.now().astimezone().utcoffset()
    set_zone("America/Chicago", vault)
    assert zone_name(vault) == "America/Chicago"
    assert today(vault) == datetime.now(CHICAGO).date()
    with pytest.raises(ValueError):
        set_zone("Mars/Olympus", vault)
    assert zone_name(vault) == "America/Chicago"


def test_patterns_bucket_readings_by_the_wearable_day():
    per = _health_by_day([{"type": "weight", "timestamp": "2026-11-22T03:30:00+00:00", "weight_lb": 180}], CHICAGO)
    assert list(per) == ["2026-11-21"]


def test_activity_sync_records_the_zone_and_config_has_the_version(tmp_path, monkeypatch):
    vault = tmp_path / "measurements.jsonl"
    monkeypatch.setenv("UNISCALE_DB_PATH", str(vault))
    client = TestClient(app)
    day = {"date": "2026-11-21", "steps": 100, "distance_km": 0.1}
    assert client.post("/api/sync/activity", json={**day, "timezone": "Not/AZone"}).status_code == 422
    assert client.post("/api/sync/activity", json=day).status_code == 201
    assert zone_name(str(vault)) is None
    assert client.post("/api/sync/activity", json={**day, "timezone": "America/Chicago"}).status_code == 201
    assert json.loads((tmp_path / "timezone.json").read_text()) == {"timezone": "America/Chicago"}
    cfg = client.get("/api/config").json()
    from uniscale import __version__
    assert cfg["version"] == __version__ and "commit" in cfg
