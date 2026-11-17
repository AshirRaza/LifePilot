"""Phase 9D.5 API security and reliability regressions (deterministic)."""

from __future__ import annotations

import os
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lifepilot.agent.answer import AnswerClaim
from lifepilot.agent.config import reset_agent_config_cache
from lifepilot.agent.contracts import AgentResponse, AgentStatus, TimingMetadata
from lifepilot.api.rate_limit import reset_rate_limit_state
from lifepilot.api.settings import reset_api_settings_cache
from lifepilot.operations.secrets_redact import redact_secrets


@pytest.fixture()
def api_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIFEPILOT_ENV", "development")
    monkeypatch.setenv("LIFEPILOT_API_REQUIRE_AUTH", "1")
    monkeypatch.setenv("LIFEPILOT_API_KEYS", "test-secret-key")
    monkeypatch.setenv("LIFEPILOT_API_RATE_LIMIT_PER_MINUTE", "100")
    monkeypatch.setenv("LIFEPILOT_API_MAX_CONCURRENT_ANSWERS", "2")
    monkeypatch.setenv("LIFEPILOT_VERIFICATION_ENABLED", "1")
    monkeypatch.setenv("LIFEPILOT_API_REQUIRE_LLM", "0")
    reset_api_settings_cache()
    reset_agent_config_cache()
    reset_rate_limit_state()
    yield
    reset_api_settings_cache()
    reset_agent_config_cache()
    reset_rate_limit_state()


def _mock_success(*_a: Any, **_k: Any) -> AgentResponse:
    return AgentResponse(
        request_id="req_test",
        status=AgentStatus.SUCCESS,
        answer="Supported mock answer.",
        claims=[AnswerClaim(claim="Supported mock answer.", evidence_ids=["ev_abcd1234"])],
        verification={"suppressed_claims": [], "warnings": []},
        timing=TimingMetadata(total_ms=1.0, verification_ms=0.5),
        retrieval_decision="internal_only",
        jurisdiction="GB",
    )


def _mock_success_without_verification(*_a: Any, **_k: Any) -> AgentResponse:
    return AgentResponse(
        request_id="req_test",
        status=AgentStatus.SUCCESS,
        answer="Unchecked text",
        claims=[],
        verification=None,
        timing=TimingMetadata(total_ms=1.0),
        retrieval_decision="internal_only",
        jurisdiction="GB",
    )


@pytest.fixture()
def client(api_env, monkeypatch: pytest.MonkeyPatch):
    import lifepilot.api.app as api_app

    monkeypatch.setattr(api_app, "run_agent", _mock_success)
    app = api_app.create_app()
    return TestClient(app)


def test_health_unauthenticated(client: TestClient):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_answer_requires_auth(client: TestClient):
    resp = client.post(
        "/v1/answer",
        json={"question": "What is a CoS?", "jurisdiction": "GB"},
    )
    assert resp.status_code == 401


def test_answer_rejects_invalid_key(client: TestClient):
    resp = client.post(
        "/v1/answer",
        headers={"X-API-Key": "wrong"},
        json={"question": "What is a CoS?", "jurisdiction": "GB"},
    )
    assert resp.status_code == 401


def test_answer_success_uses_auto_and_verification(client: TestClient, monkeypatch):
    import lifepilot.api.app as api_app

    captured: dict[str, Any] = {}

    def capture(request, config=None):
        captured["request"] = request
        captured["config"] = config
        return _mock_success()

    monkeypatch.setattr(api_app, "run_agent", capture)
    # Recreate app binding is not needed; endpoint uses module attribute.
    resp = client.post(
        "/v1/answer",
        headers={"X-API-Key": "test-secret-key"},
        json={
            "question": "What is the Skilled Worker salary threshold?",
            "jurisdiction": "GB",
            "pathway_id": "gb_skilled_worker",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert body["verification"] is not None
    assert captured["request"].retrieval.external_mode == "auto"


def test_malformed_jurisdiction(client: TestClient):
    resp = client.post(
        "/v1/answer",
        headers={"X-API-Key": "test-secret-key"},
        json={"question": "Hello", "jurisdiction": "G B"},
    )
    assert resp.status_code == 422


def test_oversized_question(client: TestClient):
    resp = client.post(
        "/v1/answer",
        headers={"X-API-Key": "test-secret-key"},
        json={"question": "x" * 5000, "jurisdiction": "GB"},
    )
    assert resp.status_code == 422


def test_rate_limit(api_env, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIFEPILOT_API_RATE_LIMIT_PER_MINUTE", "2")
    reset_api_settings_cache()
    import lifepilot.api.app as api_app

    monkeypatch.setattr(api_app, "run_agent", _mock_success)
    client = TestClient(api_app.create_app())
    headers = {"X-API-Key": "test-secret-key"}
    payload = {"question": "CoS meaning?", "jurisdiction": "GB"}
    assert client.post("/v1/answer", headers=headers, json=payload).status_code == 200
    assert client.post("/v1/answer", headers=headers, json=payload).status_code == 200
    assert client.post("/v1/answer", headers=headers, json=payload).status_code == 429


def test_verification_missing_fails_closed(api_env, monkeypatch: pytest.MonkeyPatch):
    import lifepilot.api.app as api_app

    monkeypatch.setattr(api_app, "run_agent", _mock_success_without_verification)
    client = TestClient(api_app.create_app())
    resp = client.post(
        "/v1/answer",
        headers={"X-API-Key": "test-secret-key"},
        json={"question": "Salary?", "jurisdiction": "GB"},
    )
    assert resp.status_code == 500
    assert "Verification" in resp.json()["detail"]


def test_redact_secrets():
    text = "ANTHROPIC_API_KEY=sk-ant-secretvalue password=hunter2"
    out = redact_secrets(text)
    assert "sk-ant-secretvalue" not in out
    assert "hunter2" not in out
    assert "[REDACTED]" in out


def test_ops_status_requires_auth(client: TestClient):
    assert client.get("/v1/ops/status").status_code == 401
    resp = client.get("/v1/ops/status", headers={"X-API-Key": "test-secret-key"})
    assert resp.status_code == 200
    assert "database" in resp.json()


def _asgi_post(headers: list[tuple[bytes, bytes]], body: bytes) -> int:
    import asyncio

    from lifepilot.api.app import create_app

    app = create_app()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/answer",
        "raw_path": b"/v1/answer",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 123),
        "server": ("test", 80),
        "state": {},
    }
    messages = [{"type": "http.request", "body": body, "more_body": False}]
    sent: list[dict] = []

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    return int(start["status"])


def test_missing_content_length_rejected(client: TestClient):
    del client
    status_code = _asgi_post(
        [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"x-api-key", b"test-secret-key"),
        ],
        b'{"question":"CoS?","jurisdiction":"GB"}',
    )
    assert status_code == 411


def test_content_length_oversize_rejected(client: TestClient):
    del client
    status_code = _asgi_post(
        [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"content-length", b"999999"),
            (b"x-api-key", b"test-secret-key"),
        ],
        b'{"question":"x","jurisdiction":"GB"}',
    )
    assert status_code == 413
