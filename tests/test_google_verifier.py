"""``GoogleVerifier``: every ID token check, the signing key cache and Google outages.

Keys are generated in ``tests/google_fakes.py``, tokens are signed with PyJWT, and the JWKS document is
served through ``httpx.MockTransport``; nothing touches the network.
"""

import json
import logging
import time

import pytest
from google_fakes import (
    CLIENT_ID,
    EMAIL,
    KEY,
    KID,
    OTHER_KEY,
    SUBJECT,
    Clock,
    JwksServer,
    google_settings,
    hmac_confusion_token,
    id_token,
    make_verifier,
    public_jwk,
    unsigned_token,
)
from pydantic import ValidationError

from app.integrations.google import (
    GOOGLE_ISSUERS,
    GOOGLE_JWKS_URL,
    JWKS_MAX_BYTES,
    GoogleUnavailable,
    GoogleVerificationError,
    clean_name,
)

NONCE = "test-nonce-0123456789abcdefghijklmnop"


@pytest.fixture
def settings():
    return google_settings()


@pytest.fixture
def server() -> JwksServer:
    return JwksServer()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def verifier(server, settings, clock):
    return make_verifier(server, settings, clock)


async def rejected(verifier, token: str) -> str:
    with pytest.raises(GoogleVerificationError) as exc_info:
        await verifier.verify(token)
    return exc_info.value.category


def now() -> int:
    return int(time.time())


# Settings --------------------------------------------------------------------------------------


def test_google_settings_defaults():
    settings = google_settings(google_client_id="")
    assert settings.google_configured is False
    assert (
        settings.google_nonce_ttl_seconds,
        settings.google_signup_ttl_minutes,
        settings.google_jwks_cache_seconds,
        settings.google_clock_skew_seconds,
        settings.google_max_token_age_seconds,
    ) == (300, 10, 3600, 60, 900)
    assert google_settings().google_configured is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("google_client_id", "my-client-id"),
        ("google_client_id", "123.apps.googleusercontent.com.evil.example"),
        ("google_client_id", "has space.apps.googleusercontent.com"),
        ("google_nonce_ttl_seconds", 59),
        ("google_nonce_ttl_seconds", 901),
        ("google_signup_ttl_minutes", 1),
        ("google_signup_ttl_minutes", 31),
        ("google_jwks_cache_seconds", 59),
        ("google_jwks_cache_seconds", 86_401),
        ("google_clock_skew_seconds", -1),
        ("google_clock_skew_seconds", 301),
        ("google_max_token_age_seconds", 59),
        ("google_max_token_age_seconds", 3601),
    ],
)
def test_google_settings_are_validated(field, value):
    with pytest.raises(ValidationError):
        google_settings(**{field: value})


# A valid token ---------------------------------------------------------------------------------


async def test_a_valid_token_gives_the_verified_identity(verifier, server):
    identity = await verifier.verify(id_token(NONCE, email="Amina@Example.COM"))
    assert identity.subject == SUBJECT
    assert identity.email == "amina@example.com"
    assert identity.name == "Amina Wanjiru"
    assert identity.nonce == NONCE
    assert server.urls == [GOOGLE_JWKS_URL]
    # The subject, email and nonce stay out of repr(), so an accidental log line cannot carry them.
    assert SUBJECT not in repr(identity) and NONCE not in repr(identity) and "amina@" not in repr(identity)


@pytest.mark.parametrize("issuer", sorted(GOOGLE_ISSUERS))
async def test_both_google_issuer_forms_are_accepted(verifier, issuer):
    assert (await verifier.verify(id_token(iss=issuer))).subject == SUBJECT


async def test_email_verified_as_the_string_true_is_accepted(verifier):
    assert (await verifier.verify(id_token(email_verified="true"))).email == EMAIL


async def test_a_missing_azp_is_accepted(verifier):
    assert (await verifier.verify(id_token(azp=None))).subject == SUBJECT


# Claims ----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "category"),
    [
        pytest.param({"aud": "999-other.apps.googleusercontent.com"}, "bad_audience", id="wrong-audience"),
        pytest.param({"aud": [CLIENT_ID, "999-other.apps.googleusercontent.com"]}, "bad_audience", id="aud-list"),
        pytest.param({"iss": "https://accounts.google.com.evil.example"}, "bad_issuer", id="wrong-issuer"),
        pytest.param({"iss": None}, "missing_claim", id="missing-issuer"),
        pytest.param({"exp": now() - 3600, "iat": now() - 600}, "expired", id="expired"),
        pytest.param({"iat": now() + 3600, "exp": now() + 7200}, "issued_in_future", id="iat-in-future"),
        pytest.param({"iat": now() - 2000}, "token_too_old", id="iat-too-old"),
        pytest.param({"email_verified": False}, "unverified_email", id="email-not-verified"),
        pytest.param({"email_verified": None}, "unverified_email", id="email-verified-missing"),
        pytest.param({"email_verified": "True"}, "unverified_email", id="email-verified-other-string"),
        pytest.param({"email_verified": 1}, "unverified_email", id="email-verified-number"),
        pytest.param({"email": None}, "bad_email", id="missing-email"),
        pytest.param({"email": "not-an-email"}, "bad_email", id="email-without-at"),
        pytest.param({"email": "a@b@example.com"}, "bad_email", id="email-two-at"),
        pytest.param({"email": "a b@example.com"}, "bad_email", id="email-with-space"),
        pytest.param({"email": "a" * 250 + "@example.com"}, "bad_email", id="email-too-long"),
        pytest.param({"sub": None}, "missing_claim", id="missing-sub"),
        pytest.param({"sub": "1" * 256}, "bad_subject", id="sub-too-long"),
        pytest.param({"azp": "999-other.apps.googleusercontent.com"}, "bad_authorized_party", id="azp-mismatch"),
    ],
)
async def test_claim_failures(verifier, overrides, category):
    assert await rejected(verifier, id_token(**overrides)) == category


async def test_a_token_without_a_nonce_claim_is_rejected(verifier):
    assert await rejected(verifier, id_token(None)) == "missing_nonce"


async def test_a_token_inside_the_clock_skew_is_accepted(verifier):
    assert (await verifier.verify(id_token(iat=now() + 30))).subject == SUBJECT


# Algorithms and keys ---------------------------------------------------------------------------


async def test_alg_none_is_refused_before_any_key_is_loaded(verifier, server):
    assert await rejected(verifier, unsigned_token()) == "bad_algorithm"
    assert server.calls == 0


async def test_an_hs256_token_signed_with_the_public_key_is_refused(verifier, server):
    assert await rejected(verifier, hmac_confusion_token()) == "bad_algorithm"
    assert server.calls == 0


async def test_a_token_signed_by_another_key_is_refused(verifier):
    assert await rejected(verifier, id_token(key=OTHER_KEY)) == "bad_signature"


async def test_a_token_without_a_kid_is_refused(verifier, server):
    assert await rejected(verifier, id_token(kid=None)) == "missing_kid"
    assert server.calls == 0


@pytest.mark.parametrize("credential", ["not-a-jwt-at-all-but-long", "abc.def.ghi", "e30.e30.e30"])
async def test_malformed_credentials_are_refused(verifier, credential):
    assert await rejected(verifier, credential) in {"malformed", "bad_algorithm"}


async def test_an_unknown_kid_forces_at_most_one_refresh_per_minute(verifier, server, clock):
    await verifier.verify(id_token())
    assert server.calls == 1
    rotated = id_token(key=OTHER_KEY, kid="rotated-key")
    assert await rejected(verifier, rotated) == "unknown_key"
    assert server.calls == 2  # one forced refresh
    assert await rejected(verifier, rotated) == "unknown_key"
    assert server.calls == 2  # no second refresh inside a minute
    clock.advance(61)
    server.keys.append(public_jwk(OTHER_KEY, "rotated-key"))  # Google rotated its keys meanwhile
    assert (await verifier.verify(rotated)).subject == SUBJECT
    assert server.calls == 3
    assert set(server.urls) == {GOOGLE_JWKS_URL}


async def test_keys_are_cached_until_they_expire(verifier, server, clock, settings):
    for _ in range(3):
        await verifier.verify(id_token())
    assert server.calls == 1
    clock.advance(settings.google_jwks_cache_seconds - 1)
    await verifier.verify(id_token())
    assert server.calls == 1
    clock.advance(2)
    await verifier.verify(id_token())
    assert server.calls == 2


async def test_a_shorter_cache_control_max_age_is_honoured(verifier, server, clock):
    server.headers = {"Cache-Control": "public, max-age=120, must-revalidate, no-transform"}
    await verifier.verify(id_token())
    clock.advance(119)
    await verifier.verify(id_token())
    assert server.calls == 1
    clock.advance(2)
    await verifier.verify(id_token())
    assert server.calls == 2


async def test_a_longer_max_age_does_not_extend_the_configured_cache(server, clock):
    settings = google_settings(google_jwks_cache_seconds=300)
    verifier = make_verifier(server, settings, clock)
    server.headers = {"Cache-Control": "max-age=86400"}
    await verifier.verify(id_token())
    clock.advance(301)
    await verifier.verify(id_token())
    assert server.calls == 2


# Outages ---------------------------------------------------------------------------------------


async def test_an_outage_without_cached_keys_is_unavailable(verifier, server):
    server.fail = True
    with pytest.raises(GoogleUnavailable):
        await verifier.verify(id_token())


async def test_an_outage_with_cached_keys_uses_them_for_at_most_one_more_hour(verifier, server, clock, settings):
    await verifier.verify(id_token())
    server.fail = True
    clock.advance(settings.google_jwks_cache_seconds + 1)
    assert (await verifier.verify(id_token())).subject == SUBJECT  # stale keys, refresh failed
    assert server.calls == 2
    clock.advance(3600)
    with pytest.raises(GoogleUnavailable):
        await verifier.verify(id_token())


@pytest.mark.parametrize("status", [404, 500, 302])
async def test_an_error_or_redirect_status_is_unavailable(verifier, server, status):
    server.status = status
    server.headers = {"Location": "https://evil.example/certs"}
    with pytest.raises(GoogleUnavailable):
        await verifier.verify(id_token())
    assert server.urls == [GOOGLE_JWKS_URL]  # the redirect was not followed


async def test_a_jwks_body_over_64_kb_is_rejected(verifier, server):
    server.body = json.dumps({"keys": [public_jwk(KEY)], "padding": "x" * JWKS_MAX_BYTES}).encode()
    with pytest.raises(GoogleUnavailable):
        await verifier.verify(id_token())


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b'{"keys": "x"}', b'{"keys": []}', json.dumps({"keys": [{"kty": "EC", "kid": KID}]}).encode()],
)
async def test_an_unusable_jwks_document_is_unavailable(verifier, server, body):
    server.body = body
    with pytest.raises(GoogleUnavailable):
        await verifier.verify(id_token())


async def test_keys_for_other_algorithms_or_uses_are_ignored(server, settings):
    server.keys = [{**public_jwk(KEY), "alg": "RS512"}, {**public_jwk(OTHER_KEY, "enc-key"), "use": "enc"}]
    verifier = make_verifier(server, settings)
    with pytest.raises(GoogleUnavailable):  # no usable signing key at all
        await verifier.verify(id_token())


# Names -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Amina \t\n  Wanjiru  ", "Amina Wanjiru"),
        ("Amina\x00\x07 Wan​jiru\x7f", "Amina Wanjiru"),
        ("Ｗａｎｊｉｒｕ", "Wanjiru"),  # full width letters fold with NFKC
        ("ﬁsh farmer", "fish farmer"),
        ("", "amina"),
        ("   \t ", "amina"),
        ("\x00\x01​", "amina"),
        (None, "amina"),
        (12345, "amina"),
    ],
)
def test_names_are_cleaned(raw, expected):
    assert clean_name(raw, "amina@example.com") == expected


def test_long_names_are_cut_to_100_characters():
    cleaned = clean_name("A" * 99 + "   B" + "C" * 50, "amina@example.com")
    assert len(cleaned) <= 100 and cleaned == cleaned.strip() and cleaned.startswith("A" * 99)


async def test_the_verifier_returns_the_cleaned_name(verifier):
    assert (await verifier.verify(id_token(name="Grace\x00  Otieno\n"))).name == "Grace Otieno"
    assert (await verifier.verify(id_token(name=None))).name == "amina"


# Logs ------------------------------------------------------------------------------------------


async def test_the_credential_nonce_and_claims_are_never_logged(verifier, server, caplog):
    caplog.set_level(logging.DEBUG)
    tokens = [
        id_token(NONCE),
        id_token(NONCE, aud="999-other.apps.googleusercontent.com"),
        id_token(NONCE, email_verified=False),
        id_token(NONCE, key=OTHER_KEY),
        id_token(NONCE, kid="unknown-kid"),
        hmac_confusion_token(NONCE),
    ]
    for token in tokens:
        try:
            await verifier.verify(token)
        except GoogleVerificationError:
            pass
    log_text = caplog.text + "\n".join(str(record.args) for record in caplog.records)
    assert "google_token_rejected category=" in log_text  # categories only
    for secret in (*tokens, NONCE, SUBJECT, EMAIL, "Amina Wanjiru", CLIENT_ID):
        assert secret not in log_text
    for token in tokens:
        for part in token.split("."):
            if len(part) > 8:
                assert part not in log_text
