"""Tests for password and access-token helpers."""

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core import security
from app.core.config import Settings
from app.core.security import create_access_token, decode_token, hash_password, verify_password


def test_password_hash_round_trip() -> None:
    password_hash = hash_password("correct horse battery")
    assert verify_password("correct horse battery", password_hash)
    assert not verify_password("wrong password", password_hash)


def test_token_round_trip(monkeypatch) -> None:
    monkeypatch.setattr(security, "get_settings", lambda: Settings(jwt_secret="test-secret-for-jwt-tests-at-least-32"))

    class User:
        id = "123"
        role = "farmer"

    claims = decode_token(create_access_token(User()))
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
        decode_token(token)
