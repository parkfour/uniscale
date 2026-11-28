import json
import os
import tempfile
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from uniscale.server import app
from uniscale.counter import get_counter_state, adjust_counter, set_counter
from uniscale.places import (
    haversine_distance,
    ingest_location,
    ingest_owntracks_payload,
    compute_dwell_places,
    get_dwell_places,
    update_dwell_place,
    delete_dwell_place,
    add_custom_place,
)


@pytest.fixture
def temp_vault():
    with tempfile.TemporaryDirectory() as tmpdir:
        vault = os.path.join(tmpdir, "measurements.jsonl")
        yield vault


def _profile_counter(vault, title="D Tracker", elements=("D(out)", "D(in)")):
    with open(os.path.join(os.path.dirname(vault), "profile.json"), "w") as f:
        json.dump({"counter": {"title": title, "elements": list(elements)}}, f)


def test_counter_without_a_profile_counter_has_no_elements(temp_vault):
    state = get_counter_state(temp_vault, target_date="2026-11-14")
    assert state["title"] is None and state["elements"] == [] and state["counts"] == {}
    with pytest.raises(ValueError):
        adjust_counter("d_out", 1, db_path=temp_vault)


def test_counter_elements_come_from_the_profile(temp_vault):
    _profile_counter(temp_vault, "Swearing", ["F", "S", "C", "P"])
    s = adjust_counter("F", 2, db_path=temp_vault, target_date="2026-11-14")
    s = adjust_counter("p", 1, db_path=temp_vault, target_date="2026-11-14")
    assert s["title"] == "Swearing"
    assert [e["key"] for e in s["elements"]] == ["f", "s", "c", "p"]
    assert s["counts"] == {"f": 2, "s": 0, "c": 0, "p": 1}
    assert s["daily_history"][0]["total"] == 3
    with pytest.raises(ValueError):
        adjust_counter("d_out", 1, db_path=temp_vault)


def test_counter_set_rejects_duplicate_or_empty_elements():
    from pydantic import ValidationError
    from uniscale.config import CounterSet
    with pytest.raises(ValidationError):
        CounterSet(title="X", elements=["a b", "A-B"])
    with pytest.raises(ValidationError):
        CounterSet(title="X", elements=["()"])
    with pytest.raises(ValidationError):
        CounterSet(title="X", elements=[])


def test_counter_adjust_and_get(temp_vault):
    _profile_counter(temp_vault)
    state0 = get_counter_state(temp_vault, target_date="2026-11-14")
    assert state0["counts"] == {"d_out": 0, "d_in": 0}

    s1 = adjust_counter("D(out)", 1, db_path=temp_vault, target_date="2026-11-14")
    assert s1["counts"] == {"d_out": 1, "d_in": 0}
    assert s1["totals"]["d_out"] == 1

    s2 = adjust_counter("d_in", 2, db_path=temp_vault, target_date="2026-11-14")
    assert s2["counts"] == {"d_out": 1, "d_in": 2}

    s3 = adjust_counter("d_out", -1, db_path=temp_vault, target_date="2026-11-14")
    assert s3["counts"]["d_out"] == 0

    # Never below zero
    s4 = adjust_counter("d_out", -5, db_path=temp_vault, target_date="2026-11-14")
    assert s4["counts"]["d_out"] == 0

    s5 = set_counter({"D(out)": 10, "d_in": 5}, db_path=temp_vault, target_date="2026-11-14")
    assert s5["counts"] == {"d_out": 10, "d_in": 5}
    assert s5["daily_history"][0] | {"updated_at": None} == {"date": "2026-11-14", "counts": {"d_out": 10, "d_in": 5}, "total": 15, "updated_at": None}


def test_places_haversine():
    # Distance between San Francisco and Oakland ~ 13 km
    d = haversine_distance(37.7749, -122.4194, 37.8044, -122.2712)
    assert 12000 < d < 15000

    # Distance to self is 0
    assert haversine_distance(37.7749, -122.4194, 37.7749, -122.4194) == 0.0


def test_places_dwell_clustering(temp_vault):
    target_date = "2026-11-13"
    base_epoch = int(datetime(2026, 11, 13, 9, 0, 0, tzinfo=timezone.utc).timestamp())

    # Simulate 1.5 hours of stationary points at Home (37.7749, -122.4194)
    for i in range(10):
        t = base_epoch + (i * 600)  # every 10 mins up to 90 mins (5400s)
        ingest_location(
            lat=37.7749 + (i * 0.00001),  # slight GPS jitter ~ 1m
            lon=-122.4194 - (i * 0.00001),
            epoch=t,
            accuracy=10.0,
            db_path=temp_vault
        )

    # Simulate a brief 15-minute stop elsewhere (< 1 hr, should not be included)
    stop_epoch = base_epoch + 7200
    for i in range(3):
        ingest_location(
            lat=37.8000,
            lon=-122.3000,
            epoch=stop_epoch + (i * 300),
            accuracy=10.0,
            db_path=temp_vault
        )

    # Then on to a third spot 15 minutes later, where the day ends
    ingest_location(lat=37.7000, lon=-122.2000, epoch=stop_epoch + 900, accuracy=10.0, db_path=temp_vault)

    places = compute_dwell_places(temp_vault, target_date=target_date, min_dwell_seconds=3600)
    places.sort(key=lambda p: p["start_time"])
    assert len(places) == 2  # home, and the last spot (until midnight); not the 15-minute stop
    assert all(round(p["avg_lat"], 2) != 37.80 for p in places)
    place = places[0]
    assert place["date"] == target_date
    assert place["duration_seconds"] == 7200  # until the report from the stop
    assert "2h" in place["dwell_formatted"]
    assert place["confidence_range"] >= 50.0

    # Test updating place with POI
    updated = update_dwell_place(
        place["id"],
        name="Home Studio",
        poi_type="residence",
        candidate_pois=["Home Studio", "Park"],
        db_path=temp_vault
    )
    assert updated["name"] == "Home Studio"
    assert updated["candidate_pois"] == ["Home Studio", "Park"]

    # Test query
    res = get_dwell_places(temp_vault, target_date=target_date)
    assert res["count"] == 2
    assert "Home Studio" in [p["name"] for p in res["places"]]

    # Test deleting place
    delete_dwell_place(place["id"], db_path=temp_vault)
    res_after = get_dwell_places(temp_vault, target_date=target_date, include_deleted=False)
    assert res_after["count"] == 1


def test_api_counter_and_places_endpoints(monkeypatch, temp_vault):
    monkeypatch.setenv("UNISCALE_DB_PATH", temp_vault)
    _profile_counter(temp_vault)
    client = TestClient(app)

    resp = client.get("/api/counter")
    assert resp.status_code == 200
    assert resp.json()["title"] == "D Tracker"
    assert resp.json()["elements"] == [{"key": "d_out", "label": "D(out)"}, {"key": "d_in", "label": "D(in)"}]

    assert client.post("/api/counter", json={"item": "d_out", "delta": 3}).json()["counts"]["d_out"] == 3
    assert client.post("/api/counter", json={"item": "D(in)"}).json()["counts"]["d_in"] == 1
    assert client.post("/api/counter", json={"item": "nope"}).status_code == 422
    assert client.post("/api/counter", json={"counts": {"d_in": 4}, "date": "not-a-date"}).status_code == 422
    assert client.post("/api/counter", json={"counts": {"d_in": 4}, "date": "2026-11-01"}).json()["date"] == "2026-11-01"

    # The profile's counter round-trips through the profile API
    prof = client.get("/api/profile").json()
    assert prof["counter"] == {"title": "D Tracker", "elements": ["D(out)", "D(in)"], "formula": None}
    put = client.put("/api/profile", json=prof | {"counter": {"title": "D Tracker", "formula": "D(out)+D(in)=D"}})
    assert put.status_code == 200
    assert client.get("/api/counter").json()["computed"]["label"] == "D"
    assert client.put("/api/profile", json=prof | {"counter": {"title": "D", "formula": "D(out)+="}}).status_code == 422
    assert client.put("/api/profile", json=prof | {"counter": {"title": "T", "elements": ["a", "A"]}}).status_code == 422

    # 4. POST /api/owntracks webhook
    owntracks_msg = {
        "_type": "location",
        "lat": 37.7749,
        "lon": -122.4194,
        "tst": int(datetime.now(timezone.utc).timestamp()),
        "acc": 12
    }
    resp_ot = client.post("/api/owntracks", json=owntracks_msg)
    assert resp_ot.status_code == 200
    assert resp_ot.json() == []

    # 5. GET /api/places
    resp_pl = client.get("/api/places")
    assert resp_pl.status_code == 200
    assert "places" in resp_pl.json()

    # 6. GET /api/summary includes counter and places
    resp_sum = client.get("/api/summary")
    assert resp_sum.status_code == 200
    sum_data = resp_sum.json()
    assert "counter" in sum_data
    assert "places" in sum_data
    assert sum_data["counter"]["counts"] == {"d_out": 3, "d_in": 1}


def test_counter_import_history(temp_vault):
    from uniscale.counter import get_daily_history, import_historic_records
    _profile_counter(temp_vault)
    assert get_daily_history(temp_vault) == []
    n = import_historic_records([
        {"date": "2026-01-01", "d_out": 7, "d_in": 3},
        {"date": "2026-01-02", "counts": {"D(out)": 1}, "other": 9},
    ], db_path=temp_vault)
    assert n == 2
    h = get_daily_history(temp_vault, days=50)
    assert [(x["date"], x["counts"]) for x in h] == [("2026-01-02", {"d_out": 1, "d_in": 0}), ("2026-01-01", {"d_out": 7, "d_in": 3})]


def test_counter_history_api(temp_vault, monkeypatch):
    monkeypatch.setenv("UNISCALE_DB_PATH", temp_vault)
    _profile_counter(temp_vault)
    client = TestClient(app)

    # 1. POST /api/counter/history import of 14 days
    from datetime import date, timedelta
    today = date.today()
    entries = [{"date": (today - timedelta(days=i)).isoformat(), "d_out": 2, "d_in": 1} for i in range(1, 15)]
    assert client.post("/api/counter/history", json={"entries": entries}).json()["count"] == 14

    # 2. GET /api/counter/history
    resp_hist = client.get("/api/counter/history?days=14")
    assert resp_hist.status_code == 200
    hist = resp_hist.json()["history"]
    assert len(hist) == 14

    # 3. GET /api/counter contains daily_history
    resp_state = client.get("/api/counter")
    assert resp_state.status_code == 200
    assert "daily_history" in resp_state.json()
    assert len(resp_state.json()["daily_history"]) == 14

    # 4. POST /api/counter/history import
    resp_imp = client.post("/api/counter/history", json={
        "entries": [
            {"date": "2026-02-15", "d_out": 4, "d_in": 2}
        ]
    })
    assert resp_imp.status_code == 200
    assert resp_imp.json()["count"] == 1


def test_all_places_indexes_yesterdays_owntracks_points(temp_vault):
    from datetime import datetime, timedelta, timezone
    from uniscale.places import get_dwell_places, ingest_location

    noon = datetime.now(timezone.utc).astimezone().replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=1)
    for i in range(10):  # 90 minutes in one spot yesterday
        ingest_location(lat=37.7749, lon=-122.4194, epoch=int(noon.timestamp()) + i * 600, db_path=temp_vault)

    res = get_dwell_places(temp_vault, target_date="all")
    assert res["count"] == 1
    assert res["places"][0]["date"] == noon.strftime("%Y-%m-%d")


def test_recorder_sync_turns_a_day_of_points_into_places(temp_vault, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from uniscale import places
    from uniscale.config import OwnTracksConfig

    yesterday = datetime.now(timezone.utc).astimezone().date() - timedelta(days=1)
    noon = datetime(yesterday.year, yesterday.month, yesterday.day, 12).astimezone()
    calls = []

    def fake_get(cfg, path, params):
        calls.append((path, params))
        if path == "/api/0/list":
            return {"results": ["dev1"] if "user" in params else ["me"]}
        # 90 minutes in one spot, then elsewhere
        pts = [{"_type": "location", "lat": 37.7749, "lon": -122.4194, "tst": int(noon.timestamp()) + i * 600, "acc": 10}
               for i in range(10)]
        return {"count": len(pts), "data": pts + [{"lat": 37.80, "lon": -122.30, "tst": int(noon.timestamp()) + 7000}]}

    monkeypatch.setattr(places, "_recorder_get", fake_get)
    cfg = OwnTracksConfig(recorder_url="https://recorder.example/owntracks")

    assert places.sync_recorder(temp_vault, cfg=cfg) == [yesterday.isoformat()]
    loc_call = [c for c in calls if c[0] == "/api/0/locations"][0][1]
    assert loc_call["user"] == "me" and loc_call["device"] == "dev1"

    state = places._load_places_state(temp_vault)
    assert state["recorder_synced_through"] == yesterday.isoformat()
    day_places = sorted((p for p in state["places"] if p["date"] == yesterday.isoformat()), key=lambda p: p["start_time"])
    # Stayed from noon until the report from elsewhere (7000 s), then there until midnight
    assert [p["duration_seconds"] for p in day_places][0] == 7000
    assert len(day_places) == 2

    # Already read: nothing more until another day finishes
    assert places.sync_recorder(temp_vault, cfg=cfg) == []
    # No recorder configured: no-op
    assert places.sync_recorder(temp_vault, cfg=OwnTracksConfig()) == []


def test_a_named_spot_names_later_stays_there(temp_vault):
    from datetime import datetime, timezone
    from uniscale.places import compute_dwell_places, update_dwell_place

    def stay(day, lat, lon):
        t0 = int(datetime(2026, 11, day, 9, tzinfo=timezone.utc).timestamp())
        return [{"lat": lat, "lon": lon, "epoch": t0, "accuracy": 10.0},
                {"lat": 37.70, "lon": -122.20, "epoch": t0 + 7200, "accuracy": 10.0}]

    first = compute_dwell_places(temp_vault, "2026-11-10", points=stay(10, 37.7749, -122.4194))
    home = min(first, key=lambda p: p["start_time"])
    update_dwell_place(home["id"], name="Home", db_path=temp_vault)

    later = compute_dwell_places(temp_vault, "2026-11-11", points=stay(11, 37.7750, -122.4195))
    first_later = min(later, key=lambda p: p["start_time"])
    assert first_later["name"] == "Home" and first_later["name_guessed"]


def test_stays_at_any_home_are_hidden(temp_vault):
    from datetime import datetime, timezone
    from uniscale.places import compute_dwell_places, get_dwell_places, update_dwell_place

    def stay(day, lat, lon):
        t0 = int(datetime(2026, 11, day, 9, tzinfo=timezone.utc).timestamp())
        return [{"lat": lat, "lon": lon, "epoch": t0, "accuracy": 10.0},
                {"lat": 37.70, "lon": -122.20, "epoch": t0 + 7200, "accuracy": 10.0}]

    # Two homes (moved during the year), each labelled once
    for day, (lat, lon) in [(10, (37.7749, -122.4194)), (11, (40.7128, -74.0060))]:
        first = min(compute_dwell_places(temp_vault, f"2026-11-{day}", points=stay(day, lat, lon)), key=lambda p: p["start_time"])
        update_dwell_place(first["id"], name="home", db_path=temp_vault)

    # A later stay at the first home is labelled automatically and hidden; the other spot still shows
    compute_dwell_places(temp_vault, "2026-11-12", points=stay(12, 37.7750, -122.4195))
    shown = get_dwell_places(temp_vault, target_date="all")["places"]
    assert shown and all((p.get("name") or "").lower() != "home" for p in shown)
    assert {round(p["avg_lat"], 2) for p in shown} == {37.70}
    every = get_dwell_places(temp_vault, target_date="all", include_deleted=True)["places"]
    assert sum(1 for p in every if p.get("name") == "home") == 3


def _stay(day, lat, lon, hours=2):
    """A stay at (lat, lon) on 2026-11-<day> from 09:00 UTC, then a move far away."""
    t0 = int(datetime(2026, 11, day, 9, tzinfo=timezone.utc).timestamp())
    return [{"lat": lat, "lon": lon, "epoch": t0, "accuracy": 10.0},
            {"lat": 37.0, "lon": -121.0, "epoch": t0 + hours * 3600, "accuracy": 10.0}]


def _first(places):
    return min(places, key=lambda p: p["start_time"])


def test_a_picked_name_is_remembered_guessed_and_correctable(temp_vault):
    from uniscale.places import _load_places_state

    cafe = _first(compute_dwell_places(temp_vault, "2026-11-10", points=_stay(10, 37.7749, -122.4194)))
    # Before naming: a stay from another day at the same spot has no name
    other = _first(compute_dwell_places(temp_vault, "2026-11-09", points=_stay(9, 37.7750, -122.4195)))
    update_dwell_place(cafe["id"], name="Cafe", db_path=temp_vault)

    # Named afterwards, the earlier stay is guessed too (labelled when read, not when computed)
    shown = {p["id"]: p for p in get_dwell_places(temp_vault, target_date="all")["places"]}
    assert shown[other["id"]]["name"] == "Cafe" and shown[other["id"]]["name_guessed"]
    assert shown[cafe["id"]]["name"] == "Cafe" and not shown[cafe["id"]]["name_guessed"]
    # Nowhere near: no guess
    far = _first(compute_dwell_places(temp_vault, "2026-11-11", points=_stay(11, 37.7849, -122.4194)))
    assert get_dwell_places(temp_vault, target_date="2026-11-11")["places"][0]["name"] is None

    # The guess was wrong: renaming that stay sticks, and later stays there get the new name
    update_dwell_place(other["id"], name="Library", db_path=temp_vault)
    nxt = _first(compute_dwell_places(temp_vault, "2026-11-12", points=_stay(12, 37.77495, -122.41945)))
    by_id = {p["id"]: p for p in get_dwell_places(temp_vault, target_date="all")["places"]}
    assert by_id[other["id"]]["name"] == "Library"
    assert by_id[cafe["id"]]["name"] == "Cafe"
    assert by_id[nxt["id"]]["name"] in ("Cafe", "Library")
    assert far["id"] in by_id

    # Forgetting a known place drops its guesses but not the names picked by hand
    known = _load_places_state(temp_vault)["known"]
    from uniscale.places import forget_known_place
    for k in known:
        assert forget_known_place(k["id"], temp_vault)
    by_id = {p["id"]: p for p in get_dwell_places(temp_vault, target_date="all")["places"]}
    assert by_id[nxt["id"]]["name"] is None
    assert by_id[cafe["id"]]["name"] == "Cafe"
    assert not forget_known_place("nope", temp_vault)


def test_home_is_found_from_nights_and_covers_gps_drift_and_long_stays(temp_vault):
    from uniscale.places import detect_home, _load_places_state

    home = (37.7749, -122.4194)
    nights = []
    for day in range(1, 11):  # three fixes each night, 01:00-03:00 local
        base = datetime(2026, 11, day, 1).astimezone()
        nights += [{"lat": home[0] + 0.0002 * (i - 1), "lon": home[1], "epoch": int(base.timestamp()) + i * 3600,
                    "accuracy": 20.0} for i in range(3)]
    assert detect_home(nights[:10], temp_vault) is None  # too few nights to tell
    found = detect_home(nights, temp_vault)
    assert found["name"] == "Home" and abs(found["lat"] - home[0]) < 0.001
    assert detect_home(nights, temp_vault) is None  # already known
    assert len(_load_places_state(temp_vault)["known"]) == 1

    drift = _first(compute_dwell_places(temp_vault, "2026-11-12", points=_stay(12, home[0] + 0.004, home[1])))  # ~450 m off
    long_near = _first(compute_dwell_places(temp_vault, "2026-11-13", points=_stay(13, home[0] + 0.012, home[1], hours=5)))  # 1.3 km, 5 h
    short_near = _first(compute_dwell_places(temp_vault, "2026-11-14", points=_stay(14, home[0] + 0.012, home[1])))  # 1.3 km, 2 h
    shown = {p["id"] for p in get_dwell_places(temp_vault, target_date="all")["places"]}
    assert drift["id"] not in shown and long_near["id"] not in shown and short_near["id"] in shown
    every = {p["id"]: p for p in get_dwell_places(temp_vault, target_date="all", include_home=True)["places"]}
    assert every[drift["id"]]["name"] == "Home" and every[long_near["id"]]["name"] == "Home"


def test_older_places_file_learns_its_named_stays(temp_vault):
    from uniscale.places import _places_file, _load_places_state
    os.makedirs(os.path.dirname(_places_file(temp_vault)), exist_ok=True)
    with open(_places_file(temp_vault), "w") as f:
        json.dump({"places": [{"id": "a", "date": "2026-11-01", "name": "Gym", "avg_lat": 1.0, "avg_lon": 2.0},
                              {"id": "b", "date": "2026-11-01", "name": "Home", "avg_lat": 0.0, "avg_lon": 0.0}]}, f)
    known = _load_places_state(temp_vault)["known"]
    assert [k["name"] for k in known] == ["Gym"]
    assert _load_places_state(temp_vault)["known"] == known


def test_place_suggestions_api(temp_vault, monkeypatch):
    from uniscale import places
    import uniscale.server as server
    monkeypatch.setattr(server, "get_db_path", lambda: temp_vault)
    stay = _first(compute_dwell_places(temp_vault, "2026-11-10", points=_stay(10, 37.7749, -122.4194)))
    update_dwell_place(stay["id"], name="Cafe", db_path=temp_vault)
    monkeypatch.setattr(places, "nearby_pois", lambda lat, lon, r: [{"name": "cafe", "type": "cafe", "distance_m": 20},
                                                                    {"name": "Park", "type": "park", "distance_m": 80}])
    client = TestClient(app)
    names = [s["name"] for s in client.get(f"/api/places/{stay['id']}/suggestions").json()["suggestions"]]
    assert names == ["Cafe", "Park"]  # known first, OSM duplicate dropped
    assert client.get("/api/places/nope/suggestions").status_code == 404
    kid = client.get("/api/places?date=all").json()["known"][0]["id"]
    assert client.delete(f"/api/places/known/{kid}").status_code == 200
    assert client.delete(f"/api/places/known/{kid}").status_code == 404


def test_places_map_local_zoom_in_html():
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Local Zoom" in resp.text
    assert "setPlacesMapMode" in resp.text
    assert "_placesMapMode" in resp.text


def test_cli_profile_sets_and_clears_the_counter(temp_vault, monkeypatch, capsys):
    from uniscale import cli
    from uniscale.config import load_profile
    monkeypatch.setenv("UNISCALE_DB_PATH", temp_vault)
    monkeypatch.setattr("sys.argv", ["uniscale", "profile", "--counter", "Swearing: F, S, C, P"])
    cli.main()
    assert load_profile(temp_vault).counter.elements == ["F", "S", "C", "P"]
    monkeypatch.setattr("sys.argv", ["uniscale", "profile", "--counter", ""])
    cli.main()
    assert load_profile(temp_vault).counter is None


def test_counter_formula_parses_and_evaluates():
    from uniscale.config import CounterSet, evaluate_formula
    c = CounterSet(title="Fluid", formula="Fluid(in) - Fluid(out) = Net Fluid")
    assert c.elements == ["Fluid(in)", "Fluid(out)"]
    assert c.computed["label"] == "Net Fluid" and c.computed["key"] == "net_fluid"
    assert evaluate_formula(c.computed["terms"], {"fluid_in": 5, "fluid_out": 7}) == -2
    # * and / bind before + and -; numbers are constants; division by zero gives None
    terms = CounterSet(title="X", formula="A + B*2 - C/D = R").computed["terms"]
    assert evaluate_formula(terms, {"a": 1, "b": 3, "c": 3, "d": 2}) == 5.5
    assert evaluate_formula(terms, {"a": 1, "b": 3, "c": 3, "d": 0}) is None


def test_counter_formula_rejects_bad_formulas():
    from pydantic import ValidationError
    from uniscale.config import CounterSet
    for bad in ("A+B", "A+B=C=D", "A+=C", "A+B=", "A+B=A", "-A+B=C"):
        with pytest.raises(ValidationError):
            CounterSet(title="X", formula=bad)
    with pytest.raises(ValidationError):  # a formula naming a counter the set doesn't have
        CounterSet(title="X", elements=["A"], formula="A+B=C")


def test_counter_state_carries_the_computed_value(temp_vault):
    with open(os.path.join(os.path.dirname(temp_vault), "profile.json"), "w") as f:
        json.dump({"counter": {"title": "D Tracker", "formula": "D(out)+D(in)=D"}}, f)
    adjust_counter("D(out)", 2, db_path=temp_vault, target_date="2026-11-13")
    adjust_counter("D(in)", 3, db_path=temp_vault, target_date="2026-11-14")
    s = adjust_counter("D(out)", 1, db_path=temp_vault, target_date="2026-11-14")
    assert [e["key"] for e in s["elements"]] == ["d_out", "d_in"]
    assert s["formula"] == "D(out)+D(in)=D"
    assert s["computed"] == {"key": "d", "label": "D", "value": 4, "total": 6}
    assert [h["computed"] for h in s["daily_history"]] == [4, 2]
    with pytest.raises(ValueError):  # the result is computed, never tapped
        adjust_counter("D", 1, db_path=temp_vault)

    from uniscale.patterns import daily_log, patterns_prompt
    log = daily_log(temp_vault, [], today=datetime(2026, 11, 14).date())
    assert log["counters"]["c:d"] == "D" and log["rows"][-1]["c:d"] == 4
    assert "computed each day: D(out)+D(in)=D" in patterns_prompt(log)


def test_cli_profile_sets_a_counter_formula(temp_vault, monkeypatch):
    from uniscale import cli
    from uniscale.config import load_profile
    monkeypatch.setenv("UNISCALE_DB_PATH", temp_vault)
    monkeypatch.setattr("sys.argv", ["uniscale", "profile", "--counter", "D Tracker: D(out)+D(in)=D"])
    cli.main()
    c = load_profile(temp_vault).counter
    assert c.formula == "D(out)+D(in)=D" and c.elements == ["D(out)", "D(in)"]


def test_concurrent_writers_lose_nothing(temp_vault):
    """The app's syncs arrive together: no write may clobber another's temp file or update."""
    from concurrent.futures import ThreadPoolExecutor
    from uniscale.heart_rate import add_samples, _load
    _profile_counter(temp_vault)
    now = 2_000_000_000
    with ThreadPoolExecutor(16) as pool:
        list(pool.map(lambda i: add_samples([(now - i, 60 + i % 50)], db_path=temp_vault, now=now), range(200)))
        list(pool.map(lambda i: adjust_counter("D(out)", 1, db_path=temp_vault, target_date="2026-11-28"), range(100)))
    assert len(_load(temp_vault)) == 200
    assert get_counter_state(temp_vault, "2026-11-28")["counts"]["d_out"] == 100
    assert not [f for f in os.listdir(os.path.dirname(temp_vault)) if f.endswith(".tmp")]


def test_unreadable_places_file_is_kept_aside_not_overwritten(temp_vault):
    path = os.path.join(os.path.dirname(temp_vault), "places.json")
    with open(path, "w") as f:
        f.write('{"places": [{"id": "x", "name": "Home"')  # torn write
    add_custom_place("2026-11-28", "Cafe", 30.0, -85.0, db_path=temp_vault)
    aside = [f for f in os.listdir(os.path.dirname(path)) if f.startswith("places.json.corrupt-")]
    assert len(aside) == 1
    with open(os.path.join(os.path.dirname(path), aside[0])) as f:
        assert '"name": "Home"' in f.read()
