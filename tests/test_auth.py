"""Requests to direct Lambda origin must not bypass Cloudflare authentication."""
import time
import sys
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.responses import PlainTextResponse
from starlette.testclient import TestClient

from src.auth import AccessGate, AccessVerifier


async def origin(_scope, receive, send):
    await PlainTextResponse("origin reached")(_scope, receive, send)


class StubVerifier:
    def verify(self, token):
        if token != "signed-access-token":
            raise ValueError("invalid")


class StubOriginGate(AccessGate):
    def _expected_origin_token(self):
        return "dedicated-origin-token"


class UnavailableOriginGate(StubOriginGate):
    def _expected_origin_token(self):
        raise RuntimeError("sensitive backend detail")


def test_direct_origin_is_fail_closed():
    # Standalone test origin has no ASGI lifespan handler; avoid triggering one.
    client = TestClient(StubOriginGate(origin, StubVerifier()))
    assert client.get("/health").status_code == 403
    assert client.get(
        "/health", headers={"Cf-Access-Jwt-Assertion": "signed-access-token"}
    ).status_code == 403
    assert client.get("/health", headers={"Cf-Access-Jwt-Assertion": "fake"}).status_code == 403
    both = {"Cf-Access-Jwt-Assertion": "signed-access-token", "Authorization": "Bearer dedicated-origin-token"}
    assert client.get("/health", headers={**both, "Authorization": "Bearer wrong"}).status_code == 403
    assert client.get(
        "/health", headers={"Authorization": "Bearer dedicated-origin-token"}
    ).status_code == 403
    ok_health = client.get("/health", headers=both)
    assert ok_health.status_code == 200
    assert ok_health.text == "ok"
    assert ok_health.headers["cache-control"] == "no-store"
    assert client.get("/mcp").status_code == 403
    assert client.post("/mcp", headers={"Cf-Access-Jwt-Assertion": "fake", "Authorization": "Bearer dedicated-origin-token"}).status_code == 403
    assert client.get("/other", headers={"Cf-Access-Jwt-Assertion": "signed-access-token"}).status_code == 404
    ok = client.post("/mcp", headers=both)
    assert ok.status_code == 200
    assert ok.text == "origin reached"


@pytest.mark.parametrize(
    ("gate_type", "headers", "stage", "error_type"),
    [
        (StubOriginGate, {"Authorization": "Bearer wrong-origin-token"}, "origin_bearer_mismatch", "none"),
        (UnavailableOriginGate, {}, "origin_bearer_unavailable", "none"),
        (StubOriginGate, {"Authorization": "Bearer dedicated-origin-token"}, "access_assertion_missing", "none"),
        (
            StubOriginGate,
            {
                "Authorization": "Bearer dedicated-origin-token",
                "Cf-Access-Jwt-Assertion": "fake-access-assertion",
            },
            "access_jwt_verification_failed",
            "ValueError",
        ),
    ],
)
def test_auth_rejection_logs_safe_stage_only(caplog, gate_type, headers, stage, error_type):
    client = TestClient(gate_type(origin, StubVerifier()))
    with caplog.at_level("WARNING", logger="src.auth"):
        response = client.post("/mcp", headers=headers)

    assert response.status_code == 403
    assert f"stage={stage}" in caplog.text
    assert f"error_type={error_type}" in caplog.text
    for secret in ("dedicated-origin-token", "wrong-origin-token", "fake-access-assertion"):
        assert secret not in caplog.text
    assert "sensitive backend detail" not in caplog.text
    assert "invalid" not in caplog.text


def test_origin_token_is_loaded_once_from_ssm_securely(monkeypatch):
    from src.auth import AccessGate

    calls = []
    fake_ssm = SimpleNamespace(
        get_parameter=lambda **kwargs: calls.append(kwargs) or {
            "Parameter": {"Value": "a-long-secret-origin-bearer"}
        }
    )
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=lambda name: fake_ssm))
    monkeypatch.setenv("ORIGIN_BEARER_PARAM", "/cookidoo-mcp/origin-bearer")
    gate = AccessGate(origin, StubVerifier())
    assert gate._expected_origin_token() == "a-long-secret-origin-bearer"
    assert gate._expected_origin_token() == "a-long-secret-origin-bearer"
    assert calls == [{"Name": "/cookidoo-mcp/origin-bearer", "WithDecryption": True}]


def test_origin_token_missing_configuration_fails_closed(monkeypatch):
    from src.auth import AccessGate

    monkeypatch.delenv("ORIGIN_BEARER_PARAM", raising=False)
    with pytest.raises(RuntimeError, match="Origin bearer parameter is not configured"):
        AccessGate(origin, StubVerifier())._expected_origin_token()


def test_missing_cloudflare_settings_fails_startup(monkeypatch):
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    with pytest.raises(ValueError):
        AccessVerifier.from_environment()


def test_validates_signature_audience_issuer_expiry(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = AccessVerifier("https://test-team.cloudflareaccess.com", "mcp-audience")
    monkeypatch.setattr(
        verifier.jwks,
        "get_signing_key_from_jwt",
        lambda _token: SimpleNamespace(key=private_key.public_key()),
    )
    now = int(time.time())
    claims = {
        "aud": ["mcp-audience"],
        "iss": "https://test-team.cloudflareaccess.com",
        "iat": now,
        "exp": now + 300,
    }
    token = jwt.encode(claims, private_key, algorithm="RS256")
    verifier.verify(token)
    bad_audience = jwt.encode({**claims, "aud": "another-app"}, private_key, algorithm="RS256")
    with pytest.raises(jwt.InvalidAudienceError):
        verifier.verify(bad_audience)
    expired = jwt.encode({**claims, "exp": now - 10}, private_key, algorithm="RS256")
    with pytest.raises(jwt.ExpiredSignatureError):
        verifier.verify(expired)
    with pytest.raises(ValueError):
        AccessVerifier("http://test-team.cloudflareaccess.com", "aud")
