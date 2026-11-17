import base64

import pytest
from fastapi.testclient import TestClient

from uniscale import server
from uniscale.genomics import build_clinical_profile


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("UNISCALE_DB_PATH", str(tmp_path / "m.jsonl"))
    monkeypatch.setattr(server.config.server, "api_token", "s3cret")
    return TestClient(server.app)


def test_routes_require_token_when_configured(client):
    assert client.get("/api/history").status_code == 401
    assert client.get("/").status_code == 401
    assert client.get("/api/history", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_health_stays_open(client):
    assert client.get("/health").status_code == 200


def test_bearer_and_basic_tokens_accepted(client):
    assert client.get("/api/history", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    basic = base64.b64encode(b"any:s3cret").decode()
    assert client.get("/api/history", headers={"Authorization": f"Basic {basic}"}).status_code == 200


def test_no_token_configured_leaves_api_open(client, monkeypatch):
    monkeypatch.setattr(server.config.server, "api_token", None)
    assert client.get("/api/history").status_code == 200


def test_chat_ignores_client_supplied_endpoint(client, monkeypatch):
    seen = {}

    def fake_chat(msgs, endpoint, model, db_path, **kwargs):
        seen["endpoint"] = endpoint
        return {"reply": "ok"}

    monkeypatch.setattr("uniscale.ai.chat_with_agent", fake_chat)
    # A hub with an assistant set up (with none, /api/chat refuses before reaching the model).
    from uniscale.config import AiConfig
    monkeypatch.setattr(server.config, "ai", AiConfig.model_validate({"endpoint": "http://llm.local:8080"}))
    client.post(
        "/api/chat",
        headers={"Authorization": "Bearer s3cret"},
        json={"messages": [{"role": "user", "content": "hi"}], "endpoint": "http://attacker.example"},
    )
    assert seen["endpoint"] == server.config.ai.endpoint


def test_genomics_without_data_reports_nothing():
    profile = build_clinical_profile()
    assert profile["variants"] == []
    assert set(profile["key_highlights"].values()) == {"Not genotyped"}


def test_query_llm_uses_openai_chat_route(monkeypatch):
    import io
    import json

    from uniscale import ai

    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["body"] = json.loads(req.data)
        return io.BytesIO(json.dumps({"choices": [{"message": {"content": "hello"}}]}).encode())

    monkeypatch.setattr(ai.urllib.request, "urlopen", fake_urlopen)
    assert ai.query_llm("hi", endpoint="http://llm:8080/", model="m") == "hello"
    assert seen["url"] == "http://llm:8080/v1/chat/completions"
    assert seen["body"]["messages"] == [{"role": "user", "content": "hi"}]

    # Trailing /v1 should not result in /v1/v1
    assert ai.query_llm("hi", endpoint="http://llm:8080/v1", model="m") == "hello"
    assert seen["url"] == "http://llm:8080/v1/chat/completions"
