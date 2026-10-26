"""Verification of Google ID tokens (Google Identity Services "credential") for Google sign in.

``GoogleVerifier`` checks a credential completely before anything else uses it:

- Signing keys come only from ``GOOGLE_JWKS_URL`` (a module constant; no URL from a request or an
  environment variable is ever fetched), over HTTPS with 5 second timeouts, no redirects and a 64 KB
  body limit. They are cached in memory for ``GOOGLE_JWKS_CACHE_SECONDS`` or a shorter
  ``Cache-Control: max-age``, behind an ``asyncio.Lock``. An unknown ``kid`` triggers at most one forced
  refresh per minute. When a refresh fails, a stale copy is used for at most one more hour; with no
  usable copy, ``GoogleUnavailable`` is raised (the API answers 503 ``google_unavailable``).
- The header must say ``alg: RS256`` and carry a ``kid``. Every other algorithm is refused before any key
  is loaded (never ``none``, never ``HS*``, so a public key can never be used as an HMAC secret).
- ``jwt.decode`` checks the signature, ``aud`` (``GOOGLE_CLIENT_ID``), ``exp`` and the required claims;
  then the issuer, subject, email, ``email_verified``, ``azp``, the ``iat`` window and the ``nonce``
  claim are checked here.

Every failure raises ``GoogleVerificationError`` with a category (``bad_signature``, ``expired`` and so
on). Only the category is ever logged: never the credential, the nonce, the claims or the email.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from jwt.algorithms import RSAAlgorithm

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = frozenset({"https://accounts.google.com", "accounts.google.com"})
ALLOWED_ALGORITHM = "RS256"
JWKS_TIMEOUT_SECONDS = 5.0
JWKS_MAX_BYTES = 64 * 1024
FORCED_REFRESH_INTERVAL_SECONDS = 60.0
STALE_KEYS_GRACE_SECONDS = 3600.0
MAX_KID_LENGTH = 256
MAX_SUBJECT_LENGTH = 255
MAX_EMAIL_LENGTH = 254
MAX_NAME_LENGTH = 100
REQUIRED_CLAIMS = ["exp", "iat", "iss", "aud", "sub"]
_MAX_AGE = re.compile(r"(?:^|[,\s])max-age\s*=\s*(\d+)", re.IGNORECASE)


class GoogleUnavailable(Exception):
    """Google's signing keys cannot be loaded and no usable cached copy exists."""


class GoogleVerificationError(Exception):
    """A credential failed verification; ``category`` names the check (safe to log, nothing else is)."""

    def __init__(self, category: str) -> None:
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class GoogleIdentity:
    """The verified facts of one credential. ``subject`` and ``nonce`` are kept out of ``repr``."""

    subject: str = field(repr=False)
    email: str = field(repr=False)
    name: str
    nonce: str = field(repr=False)


def clean_name(raw: object, email: str) -> str:
    """NFKC, control and format characters removed, whitespace collapsed, at most 100 characters.

    An empty result falls back to the part of the email before ``@``.
    """
    value = unicodedata.normalize("NFKC", raw) if isinstance(raw, str) else ""
    value = "".join(
        " " if character.isspace() else character
        for character in value
        if character.isspace() or not unicodedata.category(character).startswith("C")
    )
    value = " ".join(value.split())[:MAX_NAME_LENGTH].strip()
    if value:
        return value
    return email.split("@", 1)[0][:MAX_NAME_LENGTH]


def _fail(category: str) -> GoogleVerificationError:
    logger.info("google_token_rejected category=%s", category)
    return GoogleVerificationError(category)


def _valid_email(value: object) -> bool:
    if not isinstance(value, str) or not 3 <= len(value) <= MAX_EMAIL_LENGTH or value.count("@") != 1:
        return False
    local, _, domain = value.partition("@")
    return bool(local) and "." in domain and not any(character.isspace() for character in value)


def _email_verified(value: object) -> bool:
    return value is True or value == "true"


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def parse_jwks(body: bytes) -> dict[str, RSAPublicKey]:
    """Public RSA signing keys by ``kid``; anything else in the set is ignored."""
    try:
        document = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise GoogleUnavailable("invalid_jwks") from None
    entries = document.get("keys") if isinstance(document, Mapping) else None
    if not isinstance(entries, list):
        raise GoogleUnavailable("invalid_jwks")
    keys: dict[str, RSAPublicKey] = {}
    for entry in entries:
        if not isinstance(entry, Mapping) or entry.get("kty") != "RSA" or "d" in entry:
            continue
        kid = entry.get("kid")
        if not isinstance(kid, str) or not kid or len(kid) > MAX_KID_LENGTH:
            continue
        if entry.get("alg", ALLOWED_ALGORITHM) != ALLOWED_ALGORITHM or entry.get("use", "sig") != "sig":
            continue
        try:
            key = RSAAlgorithm.from_jwk(dict(entry))
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            continue
        if isinstance(key, RSAPublicKey):
            keys[kid] = key
    if not keys:
        raise GoogleUnavailable("no_keys")
    return keys


class GoogleVerifier:
    """Verifies Google ID tokens against ``GOOGLE_CLIENT_ID``; see the module docstring.

    ``client`` is used only for ``GOOGLE_JWKS_URL``. ``clock`` (wall time, for ``iat``) and
    ``monotonic`` (for the key cache) are injectable for tests.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        settings: Settings,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._settings = settings
        self._clock = clock
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._keys: dict[str, RSAPublicKey] = {}
        self._fetched_at: float | None = None
        self._fresh_for = 0.0
        self._last_forced_refresh: float | None = None

    # Keys ------------------------------------------------------------------------------------------

    def _is_fresh(self, now: float) -> bool:
        return self._fetched_at is not None and now < self._fetched_at + self._fresh_for

    def _stale_usable(self, now: float) -> bool:
        return self._fetched_at is not None and now < self._fetched_at + self._fresh_for + STALE_KEYS_GRACE_SECONDS

    async def _download(self) -> tuple[bytes, str]:
        """The JWKS body and its Cache-Control header, or ``GoogleUnavailable``."""
        try:
            async with self._client.stream(
                "GET",
                GOOGLE_JWKS_URL,
                headers={"Accept": "application/json"},
                timeout=httpx.Timeout(JWKS_TIMEOUT_SECONDS),
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise GoogleUnavailable("status")
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > JWKS_MAX_BYTES:
                    raise GoogleUnavailable("too_large")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > JWKS_MAX_BYTES:
                        raise GoogleUnavailable("too_large")
                return bytes(body), response.headers.get("cache-control", "")
        except httpx.HTTPError as exc:
            raise GoogleUnavailable(type(exc).__name__) from None

    async def _refresh(self, now: float) -> None:
        """Replace the cached keys; on failure keep a stale copy for up to an hour, else raise."""
        try:
            body, cache_control = await self._download()
            keys = parse_jwks(body)
        except GoogleUnavailable as exc:
            if self._stale_usable(now):
                logger.warning("google_jwks_refresh_failed reason=%s using_cached_keys", exc)
                return
            logger.warning("google_jwks_unavailable reason=%s", exc)
            raise
        lifetime = float(self._settings.google_jwks_cache_seconds)
        match = _MAX_AGE.search(cache_control)
        if match is not None:
            lifetime = min(lifetime, float(match.group(1)))
        self._keys = keys
        self._fetched_at = now
        self._fresh_for = lifetime
        logger.info("google_jwks_refreshed keys=%d", len(keys))

    async def _key_for(self, kid: str) -> RSAPublicKey:
        async with self._lock:
            now = self._monotonic()
            refreshed = False
            if not self._is_fresh(now):
                await self._refresh(now)
                refreshed = True
            key = self._keys.get(kid)
            if key is None and not refreshed:
                last = self._last_forced_refresh
                if last is None or now - last >= FORCED_REFRESH_INTERVAL_SECONDS:
                    self._last_forced_refresh = now
                    await self._refresh(now)
                    key = self._keys.get(kid)
            if key is None:
                raise _fail("unknown_key")
            return key

    # Verification ----------------------------------------------------------------------------------

    async def verify(self, credential: str) -> GoogleIdentity:
        """Return the verified identity of ``credential`` or raise ``GoogleVerificationError``."""
        client_id = self._settings.google_client_id
        if not client_id:
            raise GoogleUnavailable("not_configured")
        try:
            header = jwt.get_unverified_header(credential)
        except jwt.PyJWTError:
            raise _fail("malformed") from None
        if header.get("alg") != ALLOWED_ALGORITHM:
            raise _fail("bad_algorithm")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid or len(kid) > MAX_KID_LENGTH:
            raise _fail("missing_kid")
        key = await self._key_for(kid)
        skew = self._settings.google_clock_skew_seconds
        try:
            claims = jwt.decode(
                credential,
                key,
                algorithms=[ALLOWED_ALGORITHM],
                audience=client_id,
                leeway=skew,
                options={"require": REQUIRED_CLAIMS},
            )
        except jwt.ExpiredSignatureError:
            raise _fail("expired") from None
        except jwt.InvalidAudienceError:
            raise _fail("bad_audience") from None
        except jwt.ImmatureSignatureError:
            raise _fail("issued_in_future") from None
        except jwt.MissingRequiredClaimError:
            raise _fail("missing_claim") from None
        except jwt.InvalidSignatureError:
            raise _fail("bad_signature") from None
        except jwt.PyJWTError:
            raise _fail("invalid_token") from None
        return self._check_claims(claims, client_id, skew)

    def _check_claims(self, claims: Mapping[str, Any], client_id: str, skew: int) -> GoogleIdentity:
        if claims.get("iss") not in GOOGLE_ISSUERS:
            raise _fail("bad_issuer")
        if claims.get("aud") != client_id:
            raise _fail("bad_audience")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not 1 <= len(subject) <= MAX_SUBJECT_LENGTH:
            raise _fail("bad_subject")
        email = claims.get("email")
        if not _valid_email(email):
            raise _fail("bad_email")
        if not _email_verified(claims.get("email_verified")):
            raise _fail("unverified_email")
        if "azp" in claims and claims.get("azp") != client_id:
            raise _fail("bad_authorized_party")
        issued_at = claims.get("iat")
        now = self._clock()
        if not _is_number(issued_at):
            raise _fail("bad_issued_at")
        if issued_at > now + skew:
            raise _fail("issued_in_future")
        if now - issued_at > self._settings.google_max_token_age_seconds:
            raise _fail("token_too_old")
        nonce = claims.get("nonce")
        if not isinstance(nonce, str):
            raise _fail("missing_nonce")
        email = email.lower()
        return GoogleIdentity(subject=subject, email=email, name=clean_name(claims.get("name"), email), nonce=nonce)


# The process wide verifier, used through the get_google_verifier dependency (tests override it). ---

_verifier: GoogleVerifier | None = None


def get_google_verifier() -> GoogleVerifier:
    """Return the process wide verifier (one HTTP client and one key cache), creating it on first use."""
    global _verifier
    if _verifier is None:
        client = httpx.AsyncClient(follow_redirects=False, timeout=JWKS_TIMEOUT_SECONDS, trust_env=False)
        _verifier = GoogleVerifier(client, get_settings())
    return _verifier


async def close_google_verifier() -> None:
    """Close the process wide verifier's HTTP client (application shutdown)."""
    global _verifier
    if _verifier is not None:
        await _verifier._client.aclose()
        _verifier = None
