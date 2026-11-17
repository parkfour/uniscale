import base64
import json
import os
import tempfile
import zipfile
import zlib
import pytest
from uniscale import supplements
from uniscale.supplements import log_supplement_intake, get_supplements_summary
from uniscale.sleep import get_sleep_summary
from uniscale.genomics import build_clinical_profile, parse_raw_dna_text, interpret_apoe, interpret_mthfr, import_promethease_trio
from uniscale.ai import chat_with_agent

def test_supplements_logging_and_summary(monkeypatch):
    monkeypatch.setattr(supplements, "active_stack", lambda db_path=None: [
        {"name": "Omega-3 Fish Oil", "dosage": 2000.0, "unit": "mg", "category": "supplement", "frequency": "morning"},
        {"name": "Vitamin D3 + K2", "dosage": 5000.0, "unit": "IU", "category": "vitamin", "frequency": "morning"},
    ])
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tf:
        temp_path = tf.name
    try:
        log_supplement_intake(
            name="Omega-3 Fish Oil",
            dosage=2000.0,
            unit="mg",
            category="supplement",
            frequency="morning",
            db_path=temp_path
        )
        log_supplement_intake(
            name="Vitamin D3 + K2",
            dosage=5000.0,
            unit="IU",
            category="vitamin",
            frequency="morning",
            db_path=temp_path
        )
        summary = get_supplements_summary(db_path=temp_path, days=7)
        assert summary["completed_count"] >= 2
        assert summary["completion_rate_percent"] > 0
        assert len(summary["recent_intakes"]) == 2
        # Check active checklist
        omega = next((c for c in summary["checklist"] if "Omega-3" in c["name"]), None)
        assert omega is not None
        assert omega["taken_today"] is True
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

def test_genomics_interpretation():
    apoe = interpret_apoe("TT", "CC")
    assert "ε3/ε3" in apoe["isoform"]
    assert "Optimal" in apoe["isoform"]

    mthfr = interpret_mthfr("CT", "AA")
    assert "677CT" in mthfr["status"]

    profile = build_clinical_profile({
        "rs429358": "TT", "rs7412": "CC", "rs1801133": "CT", "rs2802292": "GG",
        "rs762551": "AC", "rs4149056": "TT", "rs1815739": "CT",
    })
    assert "key_highlights" in profile
    assert len(profile["variants"]) == 6
    assert "Intermediate" in profile["key_highlights"]["caffeine_metabolism"]

def test_dr_gemma_chat_fallback():
    # When ollama is not running on test machine, should gracefully return evidence-based clinical fallback
    res = chat_with_agent([{"role": "user", "content": "Explain my PhenoAge"}])
    assert "reply" in res
    assert "Phenotypic Age" in res["reply"] or "PhenoAge" in res["reply"]
    assert res["status"] in ["success", "fallback"]

def _promethease_zip(path, genotypes):
    items = [{"rsnum": rs, "geno": f"({g[0]};{g[1]})", "magnitude": 3.0} for rs, g in genotypes.items()]
    chunk = base64.b64encode(zlib.compress(json.dumps(items).encode())).decode()
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("promethease.html", f"<script>decompressString('{chunk}')</script>")
    return str(path)


def test_promethease_trio_import(tmp_path):
    self_zip = _promethease_zip(tmp_path / "self.zip", {"rs429358": "TT", "rs7412": "CC", "rs762551": "AA"})
    father = _promethease_zip(tmp_path / "f.zip", {"rs429358": "TT", "rs7412": "CT", "rs762551": "AC"})
    mother = _promethease_zip(tmp_path / "m.zip", {"rs429358": "CT", "rs7412": "CC", "rs762551": "AA"})

    prof = import_promethease_trio(self_zip, father, mother, db_path=str(tmp_path / "db.jsonl"))

    assert prof["trio_status"]["is_phased_trio"] is True
    assert prof["trio_status"]["user_snps_total"] == 3
    assert "ε3/ε3" in prof["key_highlights"]["apoe_isoform"]
    assert prof["key_highlights"]["statin_sensitivity"] == "Not genotyped"
    caffeine = next(t for t in prof["trio_insights"] if "rs762551" in t["trait"])
    assert caffeine["finding"] == "Father AC, Mother AA -> You AA"
    apoe = next(v for v in prof["variants"] if v["gene"] == "APOE")
    assert apoe["inheritance"] == "Father TT/CT, Mother CT/CC"
    assert len(prof["top_promethease_findings"]) == 3


def test_editable_supplements_stack(tmp_path):
    db_file = tmp_path / "measurements.jsonl"
    db_path = str(db_file)

    # 1. Add compounds to stack
    supplements.add_to_stack({
        "name": "Magnesium Glycinate",
        "dosage": 200.0,
        "unit": "mg",
        "category": "mineral",
        "frequency": "evening",
        "purpose": "Slow-wave sleep and recovery"
    }, db_path=db_path)

    supplements.add_to_stack({
        "name": "Creatine Monohydrate",
        "dosage": 5000.0,
        "unit": "mg",
        "category": "supplement",
        "frequency": "daily",
        "purpose": "Cellular ATP and strength"
    }, db_path=db_path)

    stack = supplements.active_stack(db_path=db_path)
    assert len(stack) == 2
    mag = next(s for s in stack if "Magnesium" in s["name"])
    assert mag["dosage"] == 200.0
    assert mag["frequency"] == "evening"

    # 2. Update compound in stack (add_to_stack upserts by case-insensitive name)
    supplements.add_to_stack({
        "name": "magnesium glycinate",
        "dosage": 400.0,
        "unit": "mg",
        "category": "mineral",
        "frequency": "evening",
        "purpose": "Optimized sleep & relaxation"
    }, db_path=db_path)

    stack = supplements.active_stack(db_path=db_path)
    assert len(stack) == 2
    mag_updated = next(s for s in stack if "magnesium" in s["name"].lower())
    assert mag_updated["dosage"] == 400.0
    supplements.add_to_stack({"name": "Magnesium Glycinate", "dosage": 400.0}, db_path=db_path)

    # 3. Log an intake and test delete_supplement_intake
    intake = supplements.log_supplement_intake(
        name="Magnesium Glycinate",
        dosage=400.0,
        unit="mg",
        db_path=db_path
    )
    summary = supplements.get_supplements_summary(db_path=db_path)
    assert summary["completed_count"] >= 1
    assert any(c["taken_today"] for c in summary["checklist"] if "Magnesium" in c["name"])

    # Delete the intake
    deleted = supplements.delete_supplement_intake(intake["timestamp"], name="Magnesium Glycinate", db_path=db_path)
    assert deleted is True
    summary2 = supplements.get_supplements_summary(db_path=db_path)
    assert not any(c["taken_today"] for c in summary2["checklist"] if "Magnesium" in c["name"])

    # 4. Remove compound from stack
    remaining = supplements.remove_from_stack("Magnesium Glycinate", db_path=db_path)
    assert len(remaining) == 1
    assert remaining[0]["name"] == "Creatine Monohydrate"
    assert supplements.remove_from_stack("Not In Stack", db_path=db_path) is None


def test_delete_supplement_intake_only_touches_supplement_records(tmp_path):
    db_path = str(tmp_path / "measurements.jsonl")
    ts = "2026-01-01T08:00:00+00:00"
    weight = {"type": "weight", "weight_kg": 80.0, "timestamp": ts}
    supp = {"type": "supplement", "name": "Zinc", "dosage": 15.0, "unit": "mg", "timestamp": ts}
    with open(db_path, "w") as f:
        f.write(json.dumps(weight) + "\nnot-json\n" + json.dumps(supp) + "\n")

    assert supplements.delete_supplement_intake(ts, db_path=db_path) is True

    with open(db_path) as f:
        lines = f.read().splitlines()
    assert lines == [json.dumps(weight), "not-json"]
    assert not os.path.exists(db_path + ".tmp")
    assert supplements.delete_supplement_intake(ts, db_path=db_path) is False


def test_remove_missing_item_does_not_freeze_config_stack(tmp_path):
    db_path = str(tmp_path / "measurements.jsonl")
    assert supplements.remove_from_stack("Nothing", db_path=db_path) is None
    assert not os.path.exists(supplements.get_stack_file_path(db_path))


def test_supplements_modal_uses_data_attributes_not_inline_strings():
    from uniscale import server
    html = server.HTML_CONTENT
    assert "deleteIntakeLog(this.dataset.ts, this.dataset.name)" in html
    assert "JSON.parse('${" not in html
    assert "suppSlotBoxes(item.slots)" in html
    assert "quickLog(this.dataset.name, this.dataset.slot)" in html


def test_dr_gemma_supplement_prompt():
    from uniscale.ai import generate_health_summary_prompt
    mock_profile = {
        "vitals": {},
        "labs": {},
        "longevity": {},
        "sleep": {},
        "supplements": {
            "checklist": [
                {"name": "Vitamin D3 + K2", "dosage": 5000.0, "unit": "IU", "frequency": "morning", "purpose": "Bone & immune", "taken_today": True},
                {"name": "Magnesium Glycinate", "dosage": 400.0, "unit": "mg", "frequency": "evening", "purpose": "Sleep recovery", "taken_today": False}
            ],
            "active_stack": [
                {"name": "Vitamin D3 + K2", "dosage": 5000.0, "unit": "IU", "frequency": "morning", "purpose": "Bone & immune", "category": "vitamin"},
                {"name": "Magnesium Glycinate", "dosage": 400.0, "unit": "mg", "frequency": "evening", "purpose": "Sleep recovery", "category": "mineral"}
            ],
            "recent_intakes": [
                {"name": "Vitamin D3 + K2", "dosage": 5000.0, "unit": "IU", "timestamp": "2026-11-07T10:00:00Z"}
            ]
        },
        "genomics": {}
    }
    prompt = generate_health_summary_prompt(mock_profile)
    assert "Vitamin D3 + K2: 5000 IU (morning)" in prompt
    assert "Magnesium Glycinate: 400 mg (evening)" in prompt
    assert "Logged Taken Today: Vitamin D3 + K2 (5000IU)" in prompt
    assert "Supplement & Longevity Protocol Principles:" in prompt
    assert "collaborative hypotheses rather than dogmatic absolutes" in prompt
    assert "proactively propose evidence-based additions" in prompt


def test_server_supplements_stack_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from uniscale import server
    db_file = tmp_path / "test_api_m.jsonl"
    monkeypatch.setenv("UNISCALE_DB_PATH", str(db_file))
    monkeypatch.setattr(server.config.server, "api_token", None)
    client = TestClient(server.app)

    # 1. GET initial stack
    res = client.get("/api/supplements/stack")
    assert res.status_code == 200
    data = res.json()
    assert "stack" in data
    assert "suggestion_catalog" in data
    assert len(data["suggestion_catalog"]) > 0

    # 2. PUT new stack
    new_stack = [
        {"name": "Omega-3", "dosage": 2000.0, "unit": "mg", "category": "supplement", "frequency": "morning", "purpose": "Cardio"},
        {"name": "Zinc Picolinate", "dosage": 15.0, "unit": "mg", "category": "mineral", "frequency": "evening", "purpose": "Immunity"}
    ]
    res_put = client.put("/api/supplements/stack", json={"stack": new_stack})
    assert res_put.status_code == 200
    assert len(res_put.json()["stack"]) == 2

    # 3. POST single item to stack
    add_item = {"name": "Ashwagandha", "dosage": 300.0, "unit": "mg", "category": "supplement", "frequency": "evening", "purpose": "Cortisol"}
    res_add = client.post("/api/supplements/stack/item", json=add_item)
    assert res_add.status_code == 200
    assert len(res_add.json()["stack"]) == 3

    # 4. DELETE single item from stack
    res_del = client.delete("/api/supplements/stack/item/Ashwagandha")
    assert res_del.status_code == 200
    assert len(res_del.json()["stack"]) == 2
    assert not any(s["name"] == "Ashwagandha" for s in res_del.json()["stack"])

    # 5. Names containing "/" route correctly; missing names 404
    client.post("/api/supplements/stack/item", json={"name": "Omega-3 Fish Oil (EPA/DHA)", "dosage": 2000.0})
    res_slash = client.delete("/api/supplements/stack/item/Omega-3 Fish Oil (EPA/DHA)")
    assert res_slash.status_code == 200
    assert len(res_slash.json()["stack"]) == 2
    assert client.delete("/api/supplements/stack/item/Nope").status_code == 404


def test_sync_sleep_endpoint_and_deduplication(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from uniscale import server
    from uniscale.sleep import get_sleep_summary
    db_file = tmp_path / "test_sleep_m.jsonl"
    monkeypatch.setenv("UNISCALE_DB_PATH", str(db_file))
    monkeypatch.setattr(server.config.server, "api_token", None)
    client = TestClient(server.app)

    sleep_payload = {
        "date": "2026-11-08",
        "timestamp": "2026-11-08T10:52:01Z",
        "duration_seconds": 19680,
        "deep_sleep_seconds": 3600,
        "rem_sleep_seconds": 3900,
        "light_sleep_seconds": 12180,
        "awake_seconds": 1200,
        "sleep_score": 75,
        "device_name": "Garmin watch"
    }

    # First sync inserts record
    r1 = client.post("/api/sync/sleep", json=sleep_payload)
    assert r1.status_code == 201
    assert r1.json()["status"] == "saved"

    # Second identical sync detects existing record and does not duplicate
    r2 = client.post("/api/sync/sleep", json=sleep_payload)
    assert r2.status_code == 201
    assert r2.json()["status"] == "already_exists"

    # Verify summary has exactly 1 night
    summary = get_sleep_summary(str(db_file))
    assert summary["total_nights_recorded"] == 1
    assert summary["latest"]["date"] == "2026-11-08"
    assert summary["latest"]["sleep_score"] == 75
    assert summary["latest"]["duration_hours"] == 5.47

    # Verify dashboard loader deduplicates properly
    records = server.load_all_records()
    sleep_records = [r for r in records if r.get("type") == "sleep"]
    assert len(sleep_records) == 1


def test_sleep_merge_keeps_garmin_data_and_separates_naps(tmp_path, monkeypatch):
    from uniscale import server
    from uniscale.sleep import get_sleep_summary
    db_file = tmp_path / "m.jsonl"
    monkeypatch.setenv("UNISCALE_DB_PATH", str(db_file))
    garmin = {"type": "sleep", "date": "2026-11-07", "timestamp": "2026-11-07T12:00:00Z",
              "duration_seconds": 27000, "deep_sleep_seconds": 5400, "rem_sleep_seconds": 6000,
              "sleep_score": 82, "hrv_status": "BALANCED", "device_name": "Garmin"}
    # Phone record for the same night: slightly longer, no stages, only an estimated score
    phone = {"type": "sleep", "date": "2026-11-07", "timestamp": "2026-11-07T12:05:00Z",
             "duration_seconds": 27300, "deep_sleep_seconds": 0, "rem_sleep_seconds": 0,
             "light_sleep_seconds": 26000, "awake_seconds": 1300, "estimated_sleep_score": 48,
             "hrv_rmssd_ms": 41.0, "device_name": "Gadgetbridge"}
    nap = {"type": "sleep", "date": "2026-11-07", "timestamp": "2026-11-07T21:00:00Z",
           "duration_seconds": 1800, "deep_sleep_seconds": 600, "rem_sleep_seconds": 0,
           "device_name": "Gadgetbridge"}
    db_file.write_text("\n".join(json.dumps(r) for r in (phone, nap, garmin)) + "\n")

    sleeps = [r for r in server.load_all_records() if r.get("type") == "sleep"]
    night = next(r for r in sleeps if not r.get("nap"))
    assert len(sleeps) == 2 and sum(1 for r in sleeps if r.get("nap")) == 1
    assert night["sleep_score"] == 82 and night["deep_sleep_seconds"] == 5400
    assert night["device_name"] == "Garmin"
    assert night["hrv_rmssd_ms"] == 41.0  # gap filled from the phone record
    assert server.get_summary()["latest_sleep"]["sleep_score"] == 82

    summary = get_sleep_summary(str(db_file))
    assert summary["total_nights_recorded"] == 1
    assert summary["latest"]["sleep_score"] == 82


def test_sync_sleep_computes_derived_fields(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from uniscale import server
    db_file = tmp_path / "m.jsonl"
    monkeypatch.setenv("UNISCALE_DB_PATH", str(db_file))
    monkeypatch.setattr(server.config.server, "api_token", None)
    client = TestClient(server.app)
    r = client.post("/api/sync/sleep", json={
        "date": "2026-11-08", "duration_seconds": 7200, "deep_sleep_seconds": 1200,
        "duration_hours": 99, "deep_sleep_minutes": 999, "device_name": "Phone"})
    assert r.status_code == 201
    row = json.loads(db_file.read_text().splitlines()[0])
    assert row["duration_hours"] == 2.0 and row["deep_sleep_minutes"] == 20


def _write_sleep_folder(folder, start, hrv=True):
    """A Gadgetbridge-style folder with one synthetic night (no real device data)."""
    import sys
    from datetime import timedelta
    sys.path.insert(0, os.path.dirname(__file__))
    from fit_builder import sleep_fit, hrv_fit
    # 10 min to fall asleep, then light/deep/REM/awake/light until the stop 7 h after the start
    stages = [(600, 2), (3000, 3), (6000, 4), (8000, 1), (8400, 2), (25200, 0)]
    stamp = (start + timedelta(hours=7)).strftime("%Y-%m-%d_%H-%M-%S")
    os.makedirs(folder, exist_ok=True)
    sleep_path = os.path.join(folder, f"SLEEP_{stamp}_1.fit")
    with open(sleep_path, "wb") as f:
        f.write(sleep_fit(start, stages, score=70))
    hrv_path = None
    if hrv:
        hrv_path = os.path.join(folder, f"HRV_STATUS_{stamp}_1.fit")
        with open(hrv_path, "wb") as f:
            f.write(hrv_fit(start + timedelta(hours=7), last_night_ms=40))
    return sleep_path, hrv_path


def test_parse_garmin_sleep_fit(tmp_path):
    from datetime import datetime, timezone
    from uniscale.importer import parse_garmin_sleep_fit
    sleep_path, hrv_path = _write_sleep_folder(tmp_path, datetime(2026, 1, 15, 4, 0, tzinfo=timezone.utc))

    s = parse_garmin_sleep_fit(sleep_path, hrv_path)
    assert s.duration_seconds == 25200  # start to stop, incl. the 10 min before the first stage
    assert s.light_sleep_seconds == 2400 + 16800  # last stage runs until the stop event
    assert (s.deep_sleep_seconds, s.rem_sleep_seconds, s.awake_seconds) == (3000, 2000, 400)
    assert s.sleep_score == 70 and s.deep_score == 80 and s.rem_score == 75
    assert s.hrv_rmssd_ms == 40.0 and s.hrv_status == "balanced"
    assert s.timestamp == datetime(2026, 1, 15, 11, 0, tzinfo=timezone.utc)


def test_sleep_fit_dated_by_local_wake_day(tmp_path, monkeypatch):
    import time
    from datetime import datetime, timezone
    from uniscale.importer import parse_garmin_sleep_fit
    # Sydney: starts 2026-01-15 20:00 local (09:00Z), wakes 2026-01-16 03:00 local (16:00Z)
    monkeypatch.setenv("TZ", "Australia/Sydney")
    time.tzset()
    try:
        sleep_path, _ = _write_sleep_folder(tmp_path, datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc), hrv=False)
        assert parse_garmin_sleep_fit(sleep_path).date == "2026-01-16"
    finally:
        monkeypatch.undo()
        time.tzset()


def test_import_gadgetbridge_folder_is_idempotent(tmp_path):
    from datetime import datetime, timezone
    from uniscale.importer import import_garmin_file
    folder = tmp_path / "SLEEP"
    sleep_path, hrv_path = _write_sleep_folder(folder, datetime(2026, 1, 15, 4, 0, tzinfo=timezone.utc))
    db = tmp_path / "m.jsonl"

    assert import_garmin_file(str(folder), str(db))["sleep_records_found"] == 1
    import_garmin_file(str(folder), str(db))
    rows = [json.loads(line) for line in db.read_text().splitlines()]
    assert [r["type"] for r in rows] == ["sleep"]  # no duplicate night, and no bogus "workout" from the sleep file
    assert rows[0]["hrv_rmssd_ms"] == 40.0

    with pytest.raises(ValueError, match="together with their SLEEP"):
        import_garmin_file(hrv_path, str(db))


def test_sync_sleep_fit_endpoint(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from fastapi.testclient import TestClient
    from uniscale import server
    db_file = tmp_path / "m.jsonl"
    monkeypatch.setenv("UNISCALE_DB_PATH", str(db_file))
    monkeypatch.setattr(server.config.server, "api_token", None)
    client = TestClient(server.app)
    sleep_path, hrv_path = _write_sleep_folder(tmp_path / "in", datetime(2026, 1, 15, 4, 0, tzinfo=timezone.utc))

    def post(with_hrv):
        files = {"sleep_file": ("../../escape.fit", open(sleep_path, "rb"), "application/octet-stream")}
        if with_hrv:
            files["hrv_file"] = ("../../escape.fit", open(hrv_path, "rb"), "application/octet-stream")
        return client.post("/api/sync/sleep/fit", files=files)

    r = post(with_hrv=True)
    assert r.status_code == 201 and r.json()["status"] == "saved"
    assert not (tmp_path / "escape.fit").exists()
    assert json.loads(db_file.read_text())["hrv_rmssd_ms"] == 40.0  # same names, but both files kept
    assert post(with_hrv=False).json()["status"] == "already_exists"


def test_sync_workout_keeps_watch_extras(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from uniscale import server
    db_file = tmp_path / "m.jsonl"
    monkeypatch.setenv("UNISCALE_DB_PATH", str(db_file))
    monkeypatch.setattr(server.config.server, "api_token", None)
    r = TestClient(server.app).post("/api/sync/workout", json={
        "sport": "running", "timestamp": "2026-01-15T15:00:00Z", "duration_seconds": 1800.5,
        "distance_meters": 5000.0, "steps": 5200, "avg_power": 230, "max_power": 300,
        "aerobic_training_effect": 3.2, "vo2max": 45.3, "device_name": "Garmin (Gadgetbridge)"})
    assert r.status_code == 201
    row = json.loads(db_file.read_text())
    assert row["distance_km"] == 5.0 and row["steps"] == 5200 and row["vo2max"] == 45.3


def test_a_workout_is_not_the_days_activity(tmp_path, monkeypatch):
    """A run has steps too, and its timestamp sorts after the day's date; the day's total must still win."""
    from fastapi.testclient import TestClient
    from uniscale import server
    from uniscale.ai import build_health_profile
    db_file = tmp_path / "m.jsonl"
    monkeypatch.setenv("UNISCALE_DB_PATH", str(db_file))
    monkeypatch.setattr(server.config.server, "api_token", None)
    rows = [
        {"type": "activity", "date": "2026-01-20", "steps": 18000, "distance_km": 15.0, "source": "Garmin"},
        {"type": "workout", "sport": "running", "timestamp": "2026-01-20T15:00:00Z", "steps": 6000,
         "distance_km": 6.0, "duration_seconds": 2400},
        {"sport": "walking", "timestamp": "2026-01-20T18:00:00Z", "steps": 2000, "duration_seconds": 1200},  # untyped
    ]
    db_file.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    c = TestClient(server.app)

    assert c.get("/api/summary").json()["latest_activity"]["steps"] == 18000
    assert [r["steps"] for r in c.get("/api/history?record_type=activity").json()["records"]] == [18000]
    profile = build_health_profile(str(db_file))
    assert profile["activity"]["count"] == 1 and profile["activity"]["latest"]["steps"] == 18000


def test_workouts_endpoint_groups_by_sport(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from uniscale import server
    db_file = tmp_path / "m.jsonl"
    monkeypatch.setenv("UNISCALE_DB_PATH", str(db_file))
    monkeypatch.setattr(server.config.server, "api_token", None)
    rows = [{"type": "workout", "sport": "running", "timestamp": f"2026-01-{d:02d}T15:00:00Z",
             "duration_seconds": 1800, "distance_meters": 5000} for d in range(1, 8)]
    rows.append({"type": "workout", "sport": "walking", "timestamp": "2026-01-05T09:00:00Z", "duration_seconds": 3600})
    # Older import without a type field is still a workout (it has "sport")
    rows.append({"sport": "cycling", "timestamp": "2025-12-30T10:00:00Z", "duration_seconds": 3600})
    db_file.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    c = TestClient(server.app)

    data = c.get("/api/workouts?per_sport=3").json()
    assert data["latest"]["timestamp"] == "2026-01-07T15:00:00Z"
    assert [s["sport"] for s in data["sports"]] == ["running", "walking", "cycling"]  # most recent sport first
    run = data["sports"][0]
    assert run["count"] == 7 and [w["timestamp"][8:10] for w in run["recent"]] == ["07", "06", "05"]
    assert c.get("/api/summary").json()["latest_workout"]["sport"] == "running"

    # One sport's full history, paged newest first, with totals over all of it
    page = c.get("/api/workouts?sport=running&offset=5&limit=5").json()
    assert page["total"] == 7 and page["distance_km"] == 35.0 and page["duration_seconds"] == 7 * 1800
    assert page["first"] == "2026-01-01T15:00:00Z"
    assert [w["timestamp"][8:10] for w in page["workouts"]] == ["02", "01"]
    assert c.get("/api/workouts?sport=rowing").json()["total"] == 0


def test_resent_workout_with_detail_replaces_the_summary(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from uniscale import server
    monkeypatch.setenv("UNISCALE_DB_PATH", str(tmp_path / "m.jsonl"))
    monkeypatch.setattr(server.config.server, "api_token", None)
    c = TestClient(server.app)
    summary = {"sport": "running", "timestamp": "2026-01-20T07:00:00Z", "duration_seconds": 1800.0,
               "distance_meters": 5000.0, "avg_heart_rate": 150, "device_name": "Garmin (Gadgetbridge)"}
    detail = dict(summary, training_load=120.5, recovery_time_hours=30.0,
                  hr_zones=[{"zone": 4, "name": "Threshold", "min_bpm": 150, "seconds": 1500.0}],
                  splits=[{"distance_m": 1000.0, "seconds": 360.0, "avg_hr": 145}],
                  hr_samples=[[0, 100], [60, 135], [120, 150]])

    assert c.post("/api/sync/workout", json=summary).status_code == 201
    assert c.post("/api/sync/workout", json=detail).status_code == 201

    data = c.get("/api/workouts?sport=running").json()
    assert data["total"] == 1
    w = data["workouts"][0]
    assert w["training_load"] == 120.5 and w["recovery_time_hours"] == 30.0
    assert w["hr_zones"][0]["seconds"] == 1500.0 and w["splits"][0]["avg_hr"] == 145
    assert w["hr_samples"] == [[0, 100], [60, 135], [120, 150]]


def test_pwa_files_are_public_and_data_is_not(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from uniscale import server
    monkeypatch.setenv("UNISCALE_DB_PATH", str(tmp_path / "m.jsonl"))
    monkeypatch.setattr(server.config.server, "api_token", "secret-token")
    c = TestClient(server.app)

    m = c.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
    assert m.json()["display"] == "standalone"
    for icon in m.json()["icons"]:
        r = c.get(icon["src"])
        assert r.status_code == 200 and len(r.content) > 100
    sw = c.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"] and "/api/" in sw.text
    assert c.get("/icons/../server.py").status_code in (401, 404)  # never served
    assert c.get("/icons/nope.png").status_code == 404
    assert c.get("/api/summary").status_code == 401  # everything else still needs the token
    assert c.get("/").status_code == 401


def test_checklist_has_one_entry_per_slot_and_check_offs_are_idempotent(tmp_path):
    from datetime import datetime, timezone
    db_path = str(tmp_path / "measurements.jsonl")
    supplements.save_active_stack([
        {"name": "Magnesium", "dosage": 200, "slots": ["bed", "breakfast"]},
        {"name": "Vitamin D", "dosage": 50, "frequency": "morning"},  # pre-slots item: defaults to breakfast
        {"name": "Ibuprofen", "dosage": 200, "slots": []},            # as needed
    ], db_path)

    rows = get_supplements_summary(db_path)["checklist"]
    assert [(r["name"], r["slot"]) for r in rows] == [
        ("Magnesium", "breakfast"), ("Vitamin D", "breakfast"), ("Magnesium", "bed"), ("Ibuprofen", None)]

    # The phone resends a queued check-off with the same tap time: only one record is written.
    tapped = datetime.now(timezone.utc).replace(microsecond=0)
    for _ in range(2):
        log_supplement_intake("Magnesium", 200, db_path=db_path, timestamp=tapped, slot="bed")
    with open(db_path) as f:
        assert sum(1 for line in f if "Magnesium" in line) == 1

    # An intake without a slot (CLI / older clients) ticks the earliest open slot.
    log_supplement_intake("Magnesium", 200, db_path=db_path)
    taken = {(r["name"], r["slot"]): r["taken_today"] for r in get_supplements_summary(db_path)["checklist"]}
    assert taken[("Magnesium", "bed")] and taken[("Magnesium", "breakfast")]
    assert not taken[("Vitamin D", "breakfast")]


def test_assistant_is_off_unless_configured_and_chat_is_refused_then(monkeypatch):
    from fastapi.testclient import TestClient
    from uniscale import server
    from uniscale.config import AiConfig

    # Defaults only (a fresh install): no assistant, and /api/chat sends nothing to a model.
    monkeypatch.setattr(server.config, "ai", AiConfig())
    client = TestClient(server.app)
    assert client.get("/api/assistant").json() == {"available": False, "name": "Dr. Gemma"}
    assert client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]}).status_code == 404

    # An [ai] section with an endpoint turns it on; enabled = false forces it off; the name is configurable.
    assert AiConfig.model_validate({"endpoint": "http://llm:8080"}).available
    assert not AiConfig.model_validate({"endpoint": "http://llm:8080", "enabled": False}).available
    monkeypatch.setattr(server.config, "ai", AiConfig.model_validate({"endpoint": "http://llm:8080", "name": "Doc"}))
    assert client.get("/api/assistant").json() == {"available": True, "name": "Doc"}


def test_garmin_fit_running_dynamics_and_track(tmp_path):
    from datetime import datetime, timezone
    from tests.fit_builder import workout_fit
    from uniscale.importer import parse_garmin_fit_file

    start = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    recs = [
        (0, 47.603456, -122.333456, 140),
        (30, 47.604000, -122.334000, 148),
        (60, 47.605000, -122.335000, 155),
    ]
    fit_bytes = workout_fit(start, records=recs)
    fit_path = tmp_path / "run.fit"
    fit_path.write_bytes(fit_bytes)

    w = parse_garmin_fit_file(str(fit_path))
    assert w is not None
    assert w.sport == "running"
    assert w.duration_seconds == 1800.0
    assert w.distance_meters == 5000.0
    assert w.calories == 350
    assert w.avg_heart_rate == 152
    assert w.max_heart_rate == 175
    assert w.avg_cadence == 166
    assert w.max_cadence == 182
    assert w.elevation_gain_m == 45.0
    assert w.elevation_loss_m == 40.0
    assert w.avg_power == 245
    assert w.max_power == 320
    assert w.normalized_power == 250
    assert w.aerobic_training_effect == 3.8
    assert w.anaerobic_training_effect == 2.2
    # Running dynamics:
    assert w.stride_length_m == 1.12
    assert w.ground_contact_ms == 235.0
    assert w.ground_contact_balance == 49.6
    assert w.vertical_oscillation_mm == 82.0
    assert w.vertical_ratio_percent == 7.3
    # Coordinates & track:
    assert round(w.start_lat, 4) == 47.6035
    assert round(w.start_lon, 4) == -122.3335
    assert len(w.gps_track) == 3
    assert len(w.splits) == 1
    assert w.splits[0].distance_m == 1000.0
    assert len(w.hr_samples) == 3


def test_gpx_parsing_and_metrics(tmp_path):
    from uniscale.importer import parse_gpx_file

    gpx_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <gpx version="1.1" creator="Gadgetbridge" xmlns="http://www.topografix.com/GPX/1/1"
         xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1">
      <trk>
        <name>Afternoon Run</name>
        <type>running</type>
        <trkseg>
          <trkpt lat="47.603456" lon="-122.333456">
            <ele>10.0</ele>
            <time>2026-01-15T12:00:00Z</time>
            <extensions>
              <gpxtpx:TrackPointExtension><gpxtpx:hr>142</gpxtpx:hr><gpxtpx:cad>84</gpxtpx:cad></gpxtpx:TrackPointExtension>
            </extensions>
          </trkpt>
          <trkpt lat="47.604500" lon="-122.334500">
            <ele>15.0</ele>
            <time>2026-01-15T12:01:00Z</time>
            <extensions>
              <gpxtpx:TrackPointExtension><gpxtpx:hr>150</gpxtpx:hr><gpxtpx:cad>85</gpxtpx:cad></gpxtpx:TrackPointExtension>
            </extensions>
          </trkpt>
        </trkseg>
      </trk>
    </gpx>
    """
    gpx_path = tmp_path / "activity.gpx"
    gpx_path.write_text(gpx_xml)

    w = parse_gpx_file(str(gpx_path))
    assert w is not None
    assert w.sport == "running"
    assert w.duration_seconds == 60.0
    assert w.distance_meters > 100.0
    assert w.elevation_gain_m == 5.0
    assert w.avg_heart_rate == 146
    assert w.max_heart_rate == 150
    assert w.avg_cadence in (84, 85)
    assert len(w.gps_track) == 2
    assert len(w.hr_samples) == 2


def test_sync_workout_fit_endpoint(tmp_path, monkeypatch):
    import io
    from datetime import datetime, timezone
    from fastapi.testclient import TestClient
    from tests.fit_builder import workout_fit
    from uniscale import server

    db_file = tmp_path / "m.jsonl"
    monkeypatch.setenv("UNISCALE_DB_PATH", str(db_file))
    monkeypatch.setattr(server.config.server, "api_token", None)
    client = TestClient(server.app)

    start = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    fit_bytes = workout_fit(start)

    response = client.post(
        "/api/sync/workout/fit",
        files={"file": ("morning_run.fit", io.BytesIO(fit_bytes), "application/octet-stream")}
    )
    assert response.status_code == 201
    data = response.json()
    assert data["status"] == "saved"
    assert data["workout"]["stride_length_m"] == 1.12
    assert data["workout"]["ground_contact_ms"] == 235.0
    assert data["workout"]["ground_contact_balance"] == 49.6
