"""Password policy and one-time code helper tests."""

import hashlib
import string
from uuid import uuid4

import pytest
from invite_fakes import configure_codes

from app.core.codes import (
    CODE_ALPHABET,
    DUMMY_CODE_HASH,
    format_code,
    generate_code,
    generate_token,
    hash_code,
    hash_token,
    normalize_code,
    verify_code,
)
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.password_policy import common_passwords, validate_new_password
from app.models.enums import UserRole


@pytest.fixture(autouse=True)
def codes(monkeypatch):
    return configure_codes(monkeypatch, get_settings())


def reason_for(password, role, **kwargs):
    with pytest.raises(AppError) as exc_info:
        validate_new_password(password, role, **kwargs)
    assert (exc_info.value.status_code, exc_info.value.code) == (422, "password_too_weak")
    assert password not in exc_info.value.reason
    return exc_info.value.reason


# Policy --------------------------------------------------------------------------------------


def test_admins_need_twelve_characters():
    assert reason_for("Kahawa-Tea9", UserRole.ADMIN) == "Use at least 12 characters."
    validate_new_password("Kahawa-Tea90", UserRole.ADMIN)


@pytest.mark.parametrize("role", [UserRole.FARMER, UserRole.BUYER, None])
def test_farmers_and_buyers_need_eight_characters(role):
    assert reason_for("Kahawa7", role) == "Use at least 8 characters."
    validate_new_password("Kahawa-7", role)


@pytest.mark.parametrize("role", [UserRole.ADMIN, UserRole.FARMER, UserRole.BUYER])
def test_everyone_is_capped_at_128_characters(role):
    validate_new_password("Kx9!" * 32, role)
    assert reason_for("Kx9!" * 32 + "a", role) == "Use at most 128 characters."


def test_the_common_password_list_is_large_and_lowercase():
    passwords = common_passwords()
    assert len(passwords) >= 100
    assert {"password123", "123456", "qwerty", "admin123", "letmein"} <= passwords
    assert all(password == password.lower() for password in passwords)


@pytest.mark.parametrize("password", ["password123", "PASSWORD123", "Password1234", "qwertyuiop123"])
def test_common_passwords_are_rejected_case_insensitively(password):
    assert reason_for(password, UserRole.FARMER) == "This password is too common."


@pytest.mark.parametrize(
    ("password", "kwargs"),
    [
        ("Grace.Wanjiru@example.com", {"email": "grace.wanjiru@example.com"}),
        ("GRACE.WANJIRU", {"email": "grace.wanjiru@example.com"}),
        ("grace wanjiru", {"name": "Grace Wanjiru"}),
    ],
)
def test_personal_details_are_rejected(password, kwargs):
    reason = reason_for(password, UserRole.ADMIN, **kwargs)
    assert reason == "The password must not be your email or name."


def test_the_policy_no_longer_takes_a_phone_number():
    with pytest.raises(TypeError):
        validate_new_password("254712000001xx", UserRole.ADMIN, phone="254712000001")  # type: ignore[call-arg]


def test_a_password_that_only_contains_the_name_is_allowed():
    validate_new_password("Grace-Wanjiru-2026", UserRole.ADMIN, name="Grace Wanjiru")


def test_the_role_value_string_is_accepted():
    assert reason_for("Kahawa-Tea9", "admin") == "Use at least 12 characters."


# Codes ---------------------------------------------------------------------------------------


def test_generated_codes_use_the_unambiguous_alphabet():
    for _ in range(200):
        code = generate_code()
        assert len(code) == 8
        assert set(code) <= set(CODE_ALPHABET)
    assert not set("01ILO") & set(CODE_ALPHABET)


def test_codes_are_shown_and_normalized():
    assert format_code("ABCDEFGH") == "ABCD-EFGH"
    assert normalize_code(" abcd-efgh ") == "ABCDEFGH"
    assert normalize_code("ab cd ef gh") == "ABCDEFGH"


def test_the_hash_is_an_hmac_bound_to_purpose_and_record():
    record = uuid4()
    code = generate_code()
    stored = hash_code("invite", record, code)
    assert len(stored) == 64 and code not in stored
    assert verify_code("invite", record, format_code(code).lower(), stored)
    assert not verify_code("reset", record, code, stored)
    assert not verify_code("invite", uuid4(), code, stored)
    assert not verify_code("invite", record, "ZZZZZZZZ" if code != "ZZZZZZZZ" else "YYYYYYYY", stored)


def test_the_hash_depends_on_the_pepper(monkeypatch):
    record = uuid4()
    first = hash_code("invite", record, "ABCDEFGH")
    monkeypatch.setattr(get_settings(), "invite_code_pepper", "another-test-pepper-with-32-or-more-characters")
    assert hash_code("invite", record, "ABCDEFGH") != first


@pytest.mark.parametrize("purpose", ["register", "login"])
def test_registration_and_login_codes_have_their_own_purpose(purpose):
    record = uuid4()
    stored = hash_code(purpose, record, "ABCDEFGH")
    assert verify_code(purpose, record, "abcd-efgh", stored)
    other = "login" if purpose == "register" else "register"
    assert not verify_code(other, record, "ABCDEFGH", stored)
    assert not verify_code("invite", record, "ABCDEFGH", stored)


def test_tokens_have_256_bits_and_are_stored_as_sha256():
    tokens = {generate_token() for _ in range(50)}
    assert len(tokens) == 50
    token = next(iter(tokens))
    assert len(token) >= 43 and set(token) <= set(string.ascii_letters + string.digits + "-_")
    assert hash_token(token) == hashlib.sha256(token.encode()).hexdigest()
    assert token not in hash_token(token)


def test_the_dummy_hash_never_matches():
    assert not verify_code("invite", uuid4(), "ABCDEFGH", DUMMY_CODE_HASH)


def test_hashing_without_a_pepper_is_503(monkeypatch):
    monkeypatch.setattr(get_settings(), "invite_code_pepper", "")
    with pytest.raises(AppError) as exc_info:
        hash_code("invite", uuid4(), "ABCDEFGH")
    assert (exc_info.value.status_code, exc_info.value.code) == (503, "codes_unavailable")
