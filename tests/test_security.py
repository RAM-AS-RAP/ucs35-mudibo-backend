"""Tests for password and access-token helpers."""

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core import security
from app.core.config import Settings
from app.core.security import DUMMY_HASH, create_access_token, decode_access_token, hash_password, verify_password


def test_password_hash_round_trip() -> None:
    password_hash = hash_password("correct horse battery")
    assert verify_password(password_hash, "correct horse battery")
    assert not verify_password(password_hash, "wrong password")
    assert not verify_password("not-an-argon2-hash", "anything")


def test_dummy_hash_never_authenticates() -> None:
    assert not verify_password(DUMMY_HASH, "mudibo-dummy-password-not-for-login")


def test_token_round_trip(monkeypatch) -> None:
    monkeypatch.setattr(security, "get_settings", lambda: Settings(jwt_secret="test-secret-for-jwt-tests-at-least-32"))

    claims = decode_access_token(create_access_token("123", "farmer"))
    assert claims["sub"] == "123"
    assert claims["role"] == "farmer"
    assert "exp" in claims


def test_expired_token_rejected(monkeypatch) -> None:
    settings = Settings(jwt_secret="test-secret-for-jwt-tests-at-least-32")
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    token = jwt.encode(
        {"sub": "123", "role": "farmer", "exp": datetime.now(UTC) - timedelta(seconds=1)},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(token)


def test_tampered_token_rejected(monkeypatch) -> None:
    settings = Settings(jwt_secret="test-secret-for-jwt-tests-at-least-32")
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    token = create_access_token("123", "farmer")
    parts = token.split(".")
    parts[1] = ("A" if parts[1][0] != "A" else "B") + parts[1][1:]
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(".".join(parts))


def test_unsigned_token_rejected(monkeypatch) -> None:
    settings = Settings(jwt_secret="test-secret-for-jwt-tests-at-least-32")
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    token = jwt.encode({"sub": "123", "exp": datetime.now(UTC) + timedelta(minutes=5)}, key="", algorithm="none")
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(token)


@pytest.mark.parametrize("claims", [{"sub": "123"}, {"exp": datetime.now(UTC) + timedelta(minutes=5)}])
def test_token_requires_subject_and_expiry(monkeypatch, claims) -> None:
    settings = Settings(jwt_secret="test-secret-for-jwt-tests-at-least-32")
    monkeypatch.setattr(security, "get_settings", lambda: settings)
    token = jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(token)
