"""Requests to direct Lambda origin must not bypass Cloudflare authentication."""
import time
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


def test_direct_origin_is_fail_closed():
    with TestClient(AccessGate(origin, StubVerifier())) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/mcp").status_code == 403
        assert client.post("/mcp", headers={"Cf-Access-Jwt-Assertion": "fake"}).status_code == 403
        assert client.get("/other", headers={"Cf-Access-Jwt-Assertion": "signed-access-token"}).status_code == 404
        ok = client.post("/mcp", headers={"Cf-Access-Jwt-Assertion": "signed-access-token"})
        assert ok.status_code == 200
        assert ok.text == "origin reached"


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
