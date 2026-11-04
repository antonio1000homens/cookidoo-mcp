"""Fail-closed Cloudflare Access authentication for the Lambda HTTP origin.

This validates the signed Access application JWT, not a caller-supplied identity
header or unverified cookie. The same check applies even when the Lambda
Function URL is called directly, bypassing the public Cloudflare hostname.
"""
from __future__ import annotations

import asyncio
import os
from urllib.parse import urlparse

import jwt
from jwt import PyJWKClient
from starlette.responses import PlainTextResponse


class AccessVerifier:
    """Verify RS256 JWT signature, issuer, audience and expiry."""

    def __init__(self, team_domain: str, audience: str):
        domain = team_domain.rstrip("/")
        url = urlparse(domain)
        if (
            url.scheme != "https"
            or not url.hostname
            or not url.hostname.endswith(".cloudflareaccess.com")
            or url.path not in ("", "/")
            or url.query
            or url.fragment
            or not audience.strip()
        ):
            raise ValueError("Cloudflare Access issuer/audience must be configured")
        self.issuer = domain
        self.audience = audience
        self.jwks = PyJWKClient(
            f"{domain}/cdn-cgi/access/certs", cache_jwk_set=True, lifespan=3600
        )

    @classmethod
    def from_environment(cls) -> AccessVerifier:
        return cls(
            os.environ.get("CF_ACCESS_TEAM_DOMAIN", ""),
            os.environ.get("CF_ACCESS_AUD", ""),
        )

    def verify(self, token: str) -> None:
        signing_key = self.jwks.get_signing_key_from_jwt(token).key
        jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=self.audience,
            issuer=self.issuer,
            options={"require": ["aud", "iss", "exp", "iat"]},
        )


class AccessGate:
    """Pure ASGI middleware: does not buffer Streamable HTTP responses."""

    def __init__(self, app, verifier: AccessVerifier):
        self.app = app
        self.verifier = verifier

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")
        if path == "/health" and method == "GET":
            await PlainTextResponse("ok", headers={"Cache-Control": "no-store"})(
                scope, receive, send
            )
            return

        if path not in ("/mcp", "/mcp/"):
            await PlainTextResponse("Not found", status_code=404)(scope, receive, send)
            return

        token = next(
            (
                value.decode("ascii", errors="ignore")
                for key, value in scope.get("headers", [])
                if key.lower() == b"cf-access-jwt-assertion"
            ),
            "",
        )
        if not token:
            await self._deny(scope, receive, send)
            return
        try:
            await asyncio.to_thread(self.verifier.verify, token)
        except Exception:
            # Do not log tokens, decoded claims, passwords or upstream exceptions.
            await self._deny(scope, receive, send)
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _deny(scope, receive, send):
        await PlainTextResponse(
            "Forbidden", status_code=403, headers={"Cache-Control": "no-store"}
        )(scope, receive, send)
