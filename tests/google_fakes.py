"""Fakes for the Google sign in tests: RSA keys, a JWKS endpoint and signed ID tokens. No network.

``KEY`` signs tokens with the ``kid`` ``KID``; ``OTHER_KEY`` is a second key that Google never published.
``JwksServer`` serves the public keys through ``httpx.MockTransport`` and records every request, so a
test can count key downloads and check that only ``GOOGLE_JWKS_URL`` is ever requested. ``id_token``
builds a valid token; each keyword replaces a claim, and ``None`` removes it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

from app.core.config import Settings
from app.integrations.google import GoogleVerifier

CLIENT_ID = "123456789012-agrisencetest.apps.googleusercontent.com"
KID = "test-key-1"
SUBJECT = "109876543210987654321"
EMAIL = "amina@example.com"

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_jwk(private_key: rsa.RSAPrivateKey, kid: str = KID) -> dict[str, Any]:
    jwk = json.loads(RSAAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return jwk


def public_pem(private_key: rsa.RSAPrivateKey = KEY) -> bytes:
    return private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def claims(nonce: str | None = "n" * 43, **overrides: Any) -> dict[str, Any]:
    """Valid claims; ``nonce=None`` leaves the nonce claim out."""
    now = int(time.time())
    values: dict[str, Any] = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "azp": CLIENT_ID,
        "sub": SUBJECT,
        "email": EMAIL,
        "email_verified": True,
        "name": "Amina Wanjiru",
        "iat": now,
        "exp": now + 3600,
    }
    if nonce is not None:
        values["nonce"] = nonce
    for key, value in overrides.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    return values


def id_token(
    nonce: str | None = "n" * 43, *, key: rsa.RSAPrivateKey = KEY, kid: str | None = KID, **overrides: Any
) -> str:
    headers = {"kid": kid} if kid is not None else {}
    return jwt.encode(claims(nonce, **overrides), key, algorithm="RS256", headers=headers)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unsigned_token(nonce: str = "n" * 43, **overrides: Any) -> str:
    """An ``alg: none`` token (no signature at all)."""
    header = _b64(json.dumps({"alg": "none", "typ": "JWT", "kid": KID}).encode())
    payload = _b64(json.dumps(claims(nonce, **overrides)).encode())
    return f"{header}.{payload}."


def hmac_confusion_token(nonce: str = "n" * 43, **overrides: Any) -> str:
    """An HS256 token whose HMAC secret is the published public key (the classic key confusion attack).

    PyJWT itself refuses to sign with a PEM public key as an HMAC secret, so the token is built by hand.
    """
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
    payload = _b64(json.dumps(claims(nonce, **overrides)).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = hmac.new(public_pem(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64(signature)}"


class JwksServer:
    """Serves a JWKS document; ``fail`` makes every request a connection error."""

    def __init__(self, *keys: dict[str, Any]) -> None:
        self.keys = list(keys) or [public_jwk(KEY)]
        self.urls: list[str] = []
        self.fail = False
        self.status = 200
        self.headers: dict[str, str] = {}
        self.body: bytes | None = None

    @property
    def calls(self) -> int:
        return len(self.urls)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        if self.fail:
            raise httpx.ConnectError("unreachable", request=request)
        body = self.body if self.body is not None else json.dumps({"keys": self.keys}).encode()
        return httpx.Response(self.status, content=body, headers=self.headers)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


class Clock:
    """The verifier's monotonic clock (key cache ages). Token times stay on the real wall clock, which
    PyJWT also uses for ``exp``."""

    def __init__(self) -> None:
        self.now = 1000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_verifier(server: JwksServer, settings: Settings, clock: Clock | None = None) -> GoogleVerifier:
    clock = clock or Clock()
    return GoogleVerifier(server.client(), settings, monotonic=lambda: clock.now)


def google_settings(**overrides: Any) -> Settings:
    values = {
        "_env_file": None,
        "jwt_secret": "test-only-jwt-secret-at-least-thirty-two-characters",
        "google_client_id": CLIENT_ID,
    }
    values.update(overrides)
    return Settings(**values)
