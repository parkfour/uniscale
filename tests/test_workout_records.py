from datetime import datetime, timezone

from uniscale.workout_records import badges, with_badges, workout_records

NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)


def run(ts, km, seconds, laps=(), sport="running"):
    return {"type": "workout", "sport": sport, "timestamp": ts, "distance_meters": km * 1000,
            "duration_seconds": seconds, "splits": [{"distance_m": d, "seconds": s} for d, s in laps]}


def test_records_split_this_month_from_all_time():
    old_fast = run("2025-05-01T08:00:00Z", 5, 1400, laps=[(1000, 270), (1000, 290)])
    recent = run("2026-11-10T08:00:00Z", 5, 1600, laps=[(1000, 300), (1000, 310)])
    recent_slow = run("2026-11-15T08:00:00Z", 5, 1800, laps=[(1000, 350)])
    rec = workout_records([old_fast, recent, recent_slow], now=NOW)["running"]
    assert rec["fastest_lap"]["ever"]["timestamp"] == old_fast["timestamp"]
    assert rec["fastest_lap"]["ever"]["seconds"] == 270 and rec["fastest_lap"]["ever"]["lap"] == 1
    assert rec["fastest_lap"]["month"]["timestamp"] == recent["timestamp"]
    assert rec["best_speed"]["ever"]["timestamp"] == old_fast["timestamp"]
    assert rec["best_speed"]["month"]["timestamp"] == recent["timestamp"]

    recs = workout_records([old_fast, recent, recent_slow], now=NOW)
    assert badges(old_fast, recs) == ["Fastest lap ever (lap 1)", "Best avg speed ever"]
    assert badges(recent, recs) == ["Fastest lap this month (lap 1)", "Best avg speed this month"]
    assert badges(recent_slow, recs) == []
    # A workout holding both the all-time and the month record shows only the all-time one
    only = run("2026-11-18T08:00:00Z", 5, 1300, laps=[(1000, 250)])
    assert badges(only, workout_records([only], now=NOW)) == ["Fastest lap ever (lap 1)", "Best avg speed ever"]


def test_short_laps_and_short_workouts_dont_set_records():
    # The last 300 m lap is fast but partial; a 500 m jog is too short for best average speed
    w = run("2026-11-18T08:00:00Z", 2.3, 700, laps=[(1000, 300), (1000, 305), (300, 60)])
    short = run("2026-11-19T08:00:00Z", 0.5, 100)
    rec = workout_records([w, short], now=NOW)["running"]
    assert rec["fastest_lap"]["ever"]["seconds"] == 300
    assert rec["best_speed"]["ever"]["timestamp"] == w["timestamp"]


def test_records_are_per_sport():
    bike = run("2026-11-18T08:00:00Z", 20, 2400, sport="cycling")
    walk = run("2026-11-18T09:00:00Z", 3, 2000, sport="walking")
    recs = workout_records([bike, walk], now=NOW)
    assert set(recs) == {"cycling", "walking"}
    assert with_badges([walk], recs)[0]["badges"] == ["Best avg speed ever"]


def test_workouts_api_carries_records_and_badges(tmp_path, monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from uniscale.server import app
    vault = tmp_path / "measurements.jsonl"
    vault.write_text("\n".join(json.dumps(w) for w in [
        run("2026-01-01T08:00:00Z", 5, 1400, laps=[(1000, 270)]),
        {**run("2026-01-05T08:00:00Z", 5, 1600), "hr_recovery_1min": 22, "hr_recovery_2min": 35},
    ]) + "\n")
    monkeypatch.setenv("UNISCALE_DB_PATH", str(vault))
    client = TestClient(app)
    data = client.get("/api/workouts").json()
    assert data["latest"]["badges"] == []
    assert data["sports"][0]["records"]["fastest_lap"]["ever"]["seconds"] == 270
    sport = client.get("/api/workouts", params={"sport": "running"}).json()
    assert sport["workouts"][1]["badges"] == ["Fastest lap ever (lap 1)", "Best avg speed ever"]
    assert sport["workouts"][0]["hr_recovery_2min"] == 35
    assert client.get("/api/summary").json()["latest_workout"]["badges"] == []


def test_implausible_speeds_are_not_records():
    drive = run("2026-11-18T08:00:00Z", 49, 3400, sport="walking")  # 52 km/h "walk"
    walk = run("2026-11-18T09:00:00Z", 3, 2000, sport="walking")
    rec = workout_records([drive, walk], now=NOW)["walking"]
    assert rec["best_speed"]["ever"]["timestamp"] == walk["timestamp"]


def test_a_fast_partial_last_lap_is_not_the_record():
    # The Nov 18 shape: five 1 km laps, then 161 m at a faster pace than any of them
    w = run("2026-11-18T17:45:29Z", 5.16, 1850, laps=[(1000, 383.7), (1000, 372.3), (1000, 370.8), (1000, 351.4),
                                                      (1000, 325.6), (161.44, 46.5)])
    rec = workout_records([w], now=NOW)["running"]["fastest_lap"]["ever"]
    assert rec["lap"] == 5 and rec["distance_m"] == 1000
    assert badges(w, workout_records([w], now=NOW))[0] == "Fastest lap ever (lap 5)"
