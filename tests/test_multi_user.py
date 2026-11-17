"""Multi-user mode: per-user vaults, token/proxy-header identity, fail-closed defaults."""
import base64
import json
import sys

import pytest
from fastapi.testclient import TestClient

from uniscale import cli, server
from uniscale.config import NoUserError, UniscaleConfig, normalize_user, vault_db_path

TOKENS = {"tok-alice-phone": "alice", "tok-alice-laptop": "Alice", "tok-bob": "bob"}
OMRON_CSV = "Date,Time,Systolic (mmHg),Diastolic (mmHg),Pulse (bpm)\n2026-03-01,08:00:00,121,79,62\n"


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def multi(tmp_path, monkeypatch):
    monkeypatch.setenv("UNISCALE_DB_PATH", str(tmp_path / "measurements.jsonl"))
    monkeypatch.delenv("UNISCALE_USER", raising=False)
    monkeypatch.setattr(server.config.auth, "mode", "multi")
    monkeypatch.setattr(server.config.auth, "tokens", dict(TOKENS))
    monkeypatch.setattr(server.config.auth, "trusted_proxies", ["127.0.0.1", "10.0.0.0/8"])
    monkeypatch.setattr(server.config.server, "api_token", None)
    return tmp_path


def _log(client, name, headers):
    return client.post("/api/supplements", json={"name": name, "dosage": 1.0}, headers=headers)


def _taken(client, headers):
    return {r["name"] for r in client.get("/api/supplements", headers=headers).json()["recent_intakes"]}


# --- path resolution -------------------------------------------------------------------

def test_normalize_user_accepts_names_and_rejects_path_tricks():
    assert normalize_user(" Alice ") == "alice"
    assert normalize_user("alice@example.com") == "alice@example.com"
    for bad in ["", "../bob", "a/b", ".hidden", "Alice Smith", "a\\b", "x" * 200]:
        with pytest.raises(ValueError):
            normalize_user(bad)


def test_vault_db_path_per_user_and_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("UNISCALE_DB_PATH", raising=False)
    monkeypatch.delenv("UNISCALE_USER", raising=False)
    cfg = UniscaleConfig.model_validate({"general": {"data_dir": str(tmp_path)}})
    assert vault_db_path(cfg) == str(tmp_path / "measurements.jsonl")
    assert vault_db_path(cfg, user="Alice") == str(tmp_path / "users" / "alice" / "measurements.jsonl")
    monkeypatch.setenv("UNISCALE_USER", "bob")
    assert vault_db_path(cfg) == str(tmp_path / "users" / "bob" / "measurements.jsonl")

    monkeypatch.delenv("UNISCALE_USER")
    cfg.auth.mode = "multi"
    with pytest.raises(NoUserError):
        vault_db_path(cfg)
    monkeypatch.setenv("UNISCALE_DB_PATH", str(tmp_path / "x" / "m.jsonl"))
    assert vault_db_path(cfg, user="bob") == str(tmp_path / "x" / "users" / "bob" / "measurements.jsonl")


# --- server ----------------------------------------------------------------------------

def test_tokens_for_the_same_user_share_one_vault_and_users_are_isolated(multi):
    c = TestClient(server.app)
    assert _log(c, "Creatine", _bearer("tok-alice-phone")).status_code == 201
    assert _log(c, "Zinc", _bearer("tok-bob")).status_code == 201

    assert _taken(c, _bearer("tok-alice-laptop")) == {"Creatine"}   # second device, same profile
    assert _taken(c, _bearer("tok-bob")) == {"Zinc"}
    assert (multi / "users" / "alice" / "measurements.jsonl").exists()
    assert (multi / "users" / "bob" / "measurements.jsonl").exists()
    assert not (multi / "measurements.jsonl").exists()               # nothing lands in a shared vault


def test_token_works_as_basic_auth_password_for_browsers(multi):
    c = TestClient(server.app)
    basic = {"Authorization": "Basic " + base64.b64encode(b"anything:tok-bob").decode()}
    assert c.get("/api/config", headers=basic).json()["user"] == "bob"


def test_proxy_header_trusted_only_from_trusted_proxies(multi):
    via_proxy = TestClient(server.app, client=("10.1.2.3", 40000))
    direct = TestClient(server.app, client=("203.0.113.9", 40000))
    assert _log(via_proxy, "Magnesium", {"X-Remote-User": "Alice"}).status_code == 201
    assert _taken(via_proxy, {"X-Remote-User": "alice"}) == {"Magnesium"}
    assert via_proxy.get("/api/config", headers={"X-Remote-User": "alice"}).json()["user"] == "alice"

    # The same header from anywhere else is ignored, so it can't be spoofed.
    assert direct.get("/api/supplements", headers={"X-Remote-User": "alice"}).status_code == 401


def test_multi_mode_rejects_missing_or_invalid_identity(multi):
    proxy = TestClient(server.app, client=("127.0.0.1", 40000))
    assert proxy.get("/api/supplements").status_code == 401
    assert proxy.get("/api/supplements", headers=_bearer("wrong")).status_code == 401
    res = proxy.get("/api/supplements", headers={"X-Remote-User": "../bob"})
    assert res.status_code == 403 and "Invalid user name" in res.json()["detail"]
    assert proxy.get("/health").status_code == 200


def test_single_mode_api_token_cannot_be_used_in_multi_mode(multi, monkeypatch):
    monkeypatch.setattr(server.config.server, "api_token", "shared-secret")
    c = TestClient(server.app)
    assert c.get("/api/supplements", headers=_bearer("shared-secret")).status_code == 401


def test_uploads_and_supplement_stack_are_per_user(multi):
    c = TestClient(server.app)
    res = c.post("/api/import/csv", files={"file": ("omron.csv", OMRON_CSV.encode(), "text/csv")},
                 headers=_bearer("tok-alice-phone"))
    assert res.status_code == 201, res.text
    rows = (multi / "users" / "alice" / "measurements.jsonl").read_text().splitlines()
    assert json.loads(rows[0])["systolic"] == 121

    stack = [{"name": "Omega-3", "dosage": 2000.0}]
    assert c.put("/api/supplements/stack", json={"stack": stack}, headers=_bearer("tok-bob")).status_code == 200
    assert (multi / "users" / "bob" / "supplements_stack.json").exists()
    assert not (multi / "users" / "alice" / "supplements_stack.json").exists()


# --- per-user profiles -----------------------------------------------------------------

LAB = {
    "type": "blood_test", "timestamp": "2026-03-01T14:30:00+00:00", "lab_name": "Quest Diagnostics",
    "markers": {k: {"name": k, "value": v} for k, v in {
        "total_cholesterol": 210, "hdl_cholesterol": 55, "albumin": 4.5, "creatinine": 0.9,
        "glucose": 90, "rdw": 13, "wbc": 5.5, "alkaline_phosphatase": 70,
    }.items()},
}


def test_load_profile_config_defaults_only_in_single_user_mode(tmp_path, monkeypatch):
    from uniscale.config import ProfileConfig, load_profile, save_profile
    monkeypatch.delenv("UNISCALE_DB_PATH", raising=False)
    monkeypatch.delenv("UNISCALE_USER", raising=False)
    cfg = UniscaleConfig.model_validate({"general": {"data_dir": str(tmp_path)}, "profile": {"birth_year": 1970, "sex": "male"}})
    assert load_profile(cfg=cfg).birth_year == 1970

    save_profile(ProfileConfig(birth_year=1985), cfg=cfg)          # dashboard edit wins over config
    prof = load_profile(cfg=cfg)
    assert (prof.birth_year, prof.sex) == (1985, None)

    cfg.auth.mode = "multi"                                      # owner's config never leaks to others
    bob = load_profile(db_path=vault_db_path(cfg, user="bob"), cfg=cfg)
    assert bob.birth_year is None and bob.sex is None


def test_profiles_are_per_user_and_drive_longevity(multi):
    c = TestClient(server.app)
    for tok in ("tok-alice-phone", "tok-bob"):
        users_dir = multi / "users" / TOKENS[tok].lower()
        users_dir.mkdir(parents=True, exist_ok=True)
        (users_dir / "measurements.jsonl").write_text(json.dumps(LAB) + "\n")

    alice = {"name": None, "counter": None, "date_of_birth": None, "birth_year": 1975, "sex": "female",
             "smoker": False, "diabetic": False, "bp_treated": True}
    assert c.put("/api/profile", json=alice, headers=_bearer("tok-alice-phone")).status_code == 200
    assert c.get("/api/profile", headers=_bearer("tok-alice-laptop")).json() == alice
    assert c.get("/api/profile", headers=_bearer("tok-bob")).json()["birth_year"] is None
    assert (multi / "users" / "alice" / "profile.json").exists()

    cur = c.get("/api/longevity", headers=_bearer("tok-alice-phone")).json()["current"]
    assert cur["chronological_age"] == 51 and cur["ten_year_ascvd_risk"] is not None
    assert "date of birth" in c.get("/api/longevity", headers=_bearer("tok-bob")).json()["error"]

    # Sex changes the pooled-cohort equation; unset sex gives no ASCVD rather than assuming male.
    c.put("/api/profile", json={**alice, "sex": "male"}, headers=_bearer("tok-alice-phone"))
    male = c.get("/api/longevity", headers=_bearer("tok-alice-phone")).json()["current"]["ten_year_ascvd_risk"]
    assert male != cur["ten_year_ascvd_risk"]
    c.put("/api/profile", json={**alice, "sex": None}, headers=_bearer("tok-alice-phone"))
    unset = c.get("/api/longevity", headers=_bearer("tok-alice-phone")).json()["current"]
    assert unset["ten_year_ascvd_risk"] is None and "sex" in unset["ascvd_category"]

    assert c.put("/api/profile", json={"sex": "other"}, headers=_bearer("tok-bob")).status_code == 422
    assert c.put("/api/profile", json={"birth_year": 1800}, headers=_bearer("tok-bob")).status_code == 422


def test_dr_gemma_prompt_includes_profile_and_no_none_percent(multi):
    from uniscale.ai import build_health_profile, generate_health_summary_prompt
    from uniscale.config import ProfileConfig, save_profile
    vault = vault_db_path(user="alice")
    import os
    os.makedirs(os.path.dirname(vault), exist_ok=True)
    with open(vault, "w") as f:
        f.write(json.dumps(LAB) + "\n")
    save_profile(ProfileConfig(birth_year=1975, smoker=True), vault)
    prompt = generate_health_summary_prompt(build_health_profile(vault))
    assert "Sex: not set | Risk factors: smoker" in prompt
    assert "None%" not in prompt and "N/A (Set sex in your profile)" in prompt


# --- CLI -------------------------------------------------------------------------------

def test_cli_profile_edits_only_given_fields(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("UNISCALE_DB_PATH", raising=False)
    monkeypatch.delenv("UNISCALE_CONFIG", raising=False)
    monkeypatch.setenv("UNISCALE_USER", "placeholder")
    monkeypatch.delenv("UNISCALE_USER")
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[general]\ndata_dir = "{tmp_path}"\n[auth]\nmode = "multi"\n')
    base = ["uniscale", "-c", str(cfg), "--user", "alice", "profile"]

    monkeypatch.setattr(sys, "argv", base + ["--birth-year", "1975", "--sex", "female", "--smoker"])
    cli.main()
    monkeypatch.setattr(sys, "argv", base + ["--no-smoker"])
    cli.main()
    saved = json.loads((tmp_path / "users" / "alice" / "profile.json").read_text())
    assert saved == {"name": None, "counter": None, "date_of_birth": None, "birth_year": 1975, "sex": "female",
                     "smoker": False, "diabetic": False, "bp_treated": False}

    monkeypatch.setattr(sys, "argv", base + ["--birth-year", "1700"])
    cli.main()
    assert "Invalid profile" in capsys.readouterr().out
    assert json.loads((tmp_path / "users" / "alice" / "profile.json").read_text())["birth_year"] == 1975

def test_cli_multi_mode_requires_user_and_writes_to_their_vault(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("UNISCALE_DB_PATH", raising=False)
    monkeypatch.delenv("UNISCALE_CONFIG", raising=False)
    # setenv first so monkeypatch restores the variable after --user sets it inside main().
    monkeypatch.setenv("UNISCALE_USER", "placeholder")
    monkeypatch.delenv("UNISCALE_USER")
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[general]\ndata_dir = "{tmp_path}"\n[auth]\nmode = "multi"\n')
    csv = tmp_path / "omron.csv"
    csv.write_text(OMRON_CSV)

    monkeypatch.setattr(sys, "argv", ["uniscale", "-c", str(cfg), "import-csv", str(csv)])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2 and "--user" in capsys.readouterr().err

    monkeypatch.setattr(sys, "argv", ["uniscale", "-c", str(cfg), "--user", "Alice", "import-csv", str(csv)])
    cli.main()
    assert (tmp_path / "users" / "alice" / "measurements.jsonl").exists()

    monkeypatch.setattr(sys, "argv", ["uniscale", "-c", str(cfg), "--user", "../bob", "history"])
    with pytest.raises(SystemExit):
        cli.main()
    assert not (tmp_path / "bob").exists()


def test_profile_name_and_date_of_birth(tmp_path, monkeypatch):
    from datetime import date
    from fastapi.testclient import TestClient
    from uniscale import server
    from uniscale.config import ProfileConfig

    p = ProfileConfig(name="  Alex ", date_of_birth=date(1980, 6, 15), birth_year=1999)
    assert p.name == "Alex" and p.birth_year == 1980          # the full date wins over a stale year
    assert p.age_on(date(2026, 6, 14)) == 45 and p.age_on(date(2026, 6, 15)) == 46
    assert ProfileConfig(birth_year=1980).age_on(date(2026, 1, 1)) == 46
    assert ProfileConfig(name="   ").name is None
    with pytest.raises(ValueError):
        ProfileConfig(date_of_birth=date(2999, 1, 1))

    db = tmp_path / "m.jsonl"
    db.write_text(json.dumps(LAB) + "\n")
    monkeypatch.setenv("UNISCALE_DB_PATH", str(db))
    monkeypatch.setattr(server.config.server, "api_token", None)
    c = TestClient(server.app)
    assert c.put("/api/profile", json={"name": "Alex", "date_of_birth": "1975-12-31"}).status_code == 200
    got = c.get("/api/profile").json()
    assert (got["name"], got["date_of_birth"], got["birth_year"]) == ("Alex", "1975-12-31", 1975)
    lab_day = date.fromisoformat(LAB["timestamp"][:10])
    expected = lab_day.year - 1975 - ((lab_day.month, lab_day.day) < (12, 31))
    assert c.get("/api/longevity").json()["current"]["chronological_age"] == expected
    assert c.put("/api/profile", json={"date_of_birth": "2999-01-01"}).status_code == 422
