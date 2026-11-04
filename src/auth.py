"""Fail-closed origin-bearer and Cloudflare Access checks for Lambda HTTP.

Both credentials are required: a dedicated bearer loaded from SSM and the
signed Access application JWT. The JWT is verified cryptographically, not
accepted from an identity header or unverified cookie. These checks also apply
when the Lambda Function URL is called directly.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import os
import threading
from urllib.parse import urlparse

import jwt
from jwt import PyJWKClient
from starlette.responses import PlainTextResponse

logger = logging.getLogger(__name__)


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
        self._origin_token = None
        self._origin_lock = threading.Lock()

    def _expected_origin_token(self) -> str:
        if self._origin_token is not None:
            return self._origin_token
        with self._origin_lock:
            if self._origin_token is not None:
                return self._origin_token
            parameter = os.environ.get("ORIGIN_BEARER_PARAM", "")
            if not parameter:
                raise RuntimeError("Origin bearer parameter is not configured")
            try:
                import boto3

                response = boto3.client("ssm").get_parameter(
                    Name=parameter, WithDecryption=True
                )
                value = response["Parameter"]["Value"]
                if not value:
                    raise ValueError("empty origin token")
            except Exception:
                raise RuntimeError("Origin bearer bootstrap failed") from None
            self._origin_token = value
            return value

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "")
        if path not in ("/mcp", "/mcp/", "/health"):
            await PlainTextResponse("Not found", status_code=404)(scope, receive, send)
            return
        if path == "/health" and method != "GET":
            await PlainTextResponse("Method not allowed", status_code=405)(
                scope, receive, send
            )
            return

        # Both independent credentials are required on every path. The bearer
        # proves the caller is the configured Worker; Access proves user auth.
        authorization = next(
            (value.decode("ascii", errors="ignore") for key, value in scope.get("headers", [])
             if key.lower() == b"authorization"),
            "",
        )
        supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
        try:
            expected = await asyncio.to_thread(self._expected_origin_token)
        except Exception:
            await self._deny(scope, receive, send, "origin_bearer_unavailable")
            return
        if not supplied or not hmac.compare_digest(
            supplied.encode("utf-8"), expected.encode("utf-8")
        ):
            await self._deny(scope, receive, send, "origin_bearer_mismatch")
            return

        # No unauthenticated HTTP path, including health.
        token = next(
            (
                value.decode("ascii", errors="ignore")
                for key, value in scope.get("headers", [])
                if key.lower() == b"cf-access-jwt-assertion"
            ),
            "",
        )
        if not token:
            await self._deny(scope, receive, send, "access_assertion_missing")
            return
        try:
            await asyncio.to_thread(self.verifier.verify, token)
        except Exception as error:
            # Log only the rejection stage and exception class. Never log tokens,
            # claims, credentials, or exception messages.
            await self._deny(
                scope,
                receive,
                send,
                "access_jwt_verification_failed",
                error_type=type(error).__name__,
            )
            return
        if path == "/health":
            await PlainTextResponse("ok", headers={"Cache-Control": "no-store"})(
                scope, receive, send
            )
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _deny(scope, receive, send, reason, *, error_type=None):
        logger.warning(
            "Cookidoo authentication denied: stage=%s error_type=%s",
            reason,
            error_type or "none",
        )
        await PlainTextResponse(
            "Forbidden", status_code=403, headers={"Cache-Control": "no-store"}
        )(scope, receive, send)
