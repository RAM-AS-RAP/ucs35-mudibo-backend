"""Admin invite tests without a database: creation, resend, listing, revocation, acceptance, request-code.

The database's own behaviour (partial unique indexes, the invite trigger, row locks, ``now()``) is
covered by ``test_invites_db.py``; here the SQL conditions are checked as text.
"""

import logging
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks
from invite_fakes import (
    NO_ROW,
    FakeMailer,
    RecordingDB,
    configure_codes,
    failing_mailer,
    inserted_invite,
    install,
    invite_attempt,
    person,
    rows,
    updated_invite,
)
from payment_fakes import NOW, FakeDB, FakeResult
from response_helpers import without_request_id
from sqlalchemy.exc import IntegrityError

from app.core.codes import format_code, generate_code, hash_code, verify_code
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.security import verify_password
from app.integrations import email as email_integration
from app.models.enums import UserRole
from app.models.user import User
from app.routers.auth import limiter
from app.schemas.common import Pagination
from app.schemas.invite import AcceptInviteRequest, InvitePage, InviteSentOut
from app.services import auth_service, invite_service

INVITE_BODY = {"name": " Grace Admin ", "phone_number": "0712000001", "email": "Grace@Example.com"}
GENERIC_FAILURE = {"error": {"code": "invalid_or_expired_code", "message": "The code is invalid or has expired."}}
STRONG_PASSWORD = "Mavuno-Ledger-2026!"


@pytest.fixture(autouse=True)
def codes(monkeypatch):
    return configure_codes(monkeypatch, get_settings())


@pytest.fixture
def mailer(monkeypatch):
    fake = FakeMailer()
    monkeypatch.setattr(email_integration, "send_email", fake.send_email)
    return fake


def super_admin():
    return person(super_admin=True)


def create_db(*, user_exists=False, pending=0):
    return RecordingDB(scalars=[user_exists, pending], results=[FakeResult(), FakeResult(), inserted_invite])


# Role matrix ---------------------------------------------------------------------------------

SUPER_ADMIN_ROUTES = [
    ("post", "/api/v1/admin/invites", {"name": "Grace Admin", "phone_number": "0712000001", "email": "g@example.com"}),
    ("post", f"/api/v1/admin/invites/{uuid4()}/resend", None),
    ("get", "/api/v1/admin/invites", None),
    ("delete", f"/api/v1/admin/invites/{uuid4()}", None),
    ("post", "/api/v1/admin/email/test", None),
]


async def call(api_client, method, path, body):
    if body is not None:
        return await getattr(api_client, method)(path, json=body)
    return await getattr(api_client, method)(path)


@pytest.mark.parametrize(("method", "path", "body"), SUPER_ADMIN_ROUTES)
async def test_super_admin_routes_reject_missing_tokens(api_client, application, method, path, body):
    install(application, None)
    response = await call(api_client, method, path, body)
    assert response.status_code == 401


@pytest.mark.parametrize("role", [UserRole.FARMER, UserRole.BUYER, UserRole.ADMIN])
@pytest.mark.parametrize(("method", "path", "body"), SUPER_ADMIN_ROUTES)
async def test_super_admin_routes_forbid_everyone_else(api_client, application, role, method, path, body):
    db = install(application, person(role))
    response = await call(api_client, method, path, body)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert db.statements == []


async def test_super_admin_routes_succeed_for_the_super_admin(api_client, application, monkeypatch, mailer):
    sent = InviteSentOut(
        id=uuid4(),
        name="Grace Admin",
        email_masked="g***@e***.com",
        phone_masked="254*******001",
        expires_at=NOW,
        code_expires_at=NOW,
        email_sent=True,
        message="sent",
    )

    async def returns_sent(*args, **kwargs):
        return sent

    async def returns_page(*args, **kwargs):
        return InvitePage(items=[], total=0, limit=20, offset=0)

    async def returns_none(*args, **kwargs):
        return None

    monkeypatch.setattr(invite_service, "create_invite", returns_sent)
    monkeypatch.setattr(invite_service, "resend_invite", returns_sent)
    monkeypatch.setattr(invite_service, "list_invites", returns_page)
    monkeypatch.setattr(invite_service, "revoke_invite", returns_none)
    install(application, super_admin())
    statuses = [(await call(api_client, method, path, body)).status_code for method, path, body in SUPER_ADMIN_ROUTES]
    assert statuses == [201, 200, 200, 204, 200]


async def test_public_routes_work_without_a_token(api_client, application, monkeypatch):
    async def accepted(*args, **kwargs):
        return {
            "id": uuid4(),
            "name": "Grace Admin",
            "phone_number": "254712000001",
            "email": "grace@example.com",
            "role": "admin",
            "must_change_password": False,
            "created_at": NOW,
        }

    async def nothing(*args, **kwargs):
        return None

    from app.services import password_reset_service

    monkeypatch.setattr(invite_service, "accept_invite", accepted)
    monkeypatch.setattr(invite_service, "request_invite_code", nothing)
    monkeypatch.setattr(password_reset_service, "request_reset", nothing)
    monkeypatch.setattr(password_reset_service, "confirm_reset", nothing)
    install(application, None)
    email = {"email": "grace@example.com"}
    responses = [
        await api_client.post("/api/v1/auth/accept-invite", json={**email, "code": "ABCD-EFGH", "password": "x"}),
        await api_client.post("/api/v1/auth/invite/request-code", json=email),
        await api_client.post("/api/v1/auth/password-reset/request", json=email),
        await api_client.post(
            "/api/v1/auth/password-reset/confirm", json={**email, "code": "ABCD-EFGH", "new_password": "x"}
        ),
    ]
    assert [response.status_code for response in responses] == [201, 202, 202, 204]


@pytest.mark.parametrize(
    "body",
    [
        {**INVITE_BODY, "role": "admin"},
        {**INVITE_BODY, "is_super_admin": True},
        {**INVITE_BODY, "must_change_password": False},
        {"name": "Grace Admin", "phone_number": "0712000001"},
        {**INVITE_BODY, "name": "G"},
        {**INVITE_BODY, "phone_number": "12345"},
        {**INVITE_BODY, "email": "not-an-email"},
    ],
)
async def test_invite_bodies_reject_flags_and_invalid_fields(api_client, application, body):
    db = install(application, super_admin())
    response = await api_client.post("/api/v1/admin/invites", json=body)
    assert response.status_code == 422
    assert db.statements == []


# Creation ------------------------------------------------------------------------------------


async def test_invite_creation_emails_the_code_after_commit_and_never_returns_it(
    api_client, application, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    actor = super_admin()
    db = install(application, actor, create_db())
    mailer = FakeMailer(db)
    monkeypatch.setattr(email_integration, "send_email", mailer.send_email)

    response = await api_client.post("/api/v1/admin/invites", json=INVITE_BODY)

    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {
        "id",
        "name",
        "email_masked",
        "phone_masked",
        "expires_at",
        "code_expires_at",
        "email_sent",
        "message",
    }
    assert (body["name"], body["email_masked"], body["phone_masked"]) == (
        "Grace Admin",
        "g***@e***.com",
        "254*******001",
    )
    assert body["email_sent"] is True
    code = mailer.code()
    for secret in (code, format_code(code)):
        assert secret not in response.text
        assert secret not in caplog.text
    stored = db.params_for("insert into public.admin_invites")
    assert stored["code_hash"] == hash_code("invite", stored["id"], code)
    assert code not in str(stored)
    assert (stored["email"], stored["phone"], stored["name"]) == ("grace@example.com", "254712000001", "Grace Admin")
    assert stored["created_by"] == actor.id
    assert stored["code_hash"] not in response.text and stored["code_hash"] not in caplog.text
    # The email goes out after the commit, with no transaction open.
    assert mailer.commits_at_send == [1]
    assert mailer.open_at_send == [[]]
    message = mailer.sent[0]
    assert message.to == "grace@example.com"
    assert code not in message.subject and format_code(code) not in message.subject
    assert "by Super Admin" in message.text and "https://admin.agrisence.test/accept-invite" in message.text
    assert "If you did not expect this email, ignore it." in message.text


async def test_invite_times_come_from_the_database_clock(mailer):
    db = create_db()
    await invite_service.create_invite(db.session(), super_admin(), _create_request())
    insert_sql = db.statements[db.index_of("insert into public.admin_invites")]
    assert "now() + make_interval(mins" in insert_sql and "now() + make_interval(days" in insert_sql
    assert db.params_for("insert into public.admin_invites")["days"] == 7


def _create_request():
    from app.schemas.invite import CreateInviteRequest

    return CreateInviteRequest(**INVITE_BODY)


async def test_an_existing_user_gives_409_and_creates_nothing(mailer):
    db = create_db(user_exists=True)
    with pytest.raises(AppError) as exc_info:
        await invite_service.create_invite(db.session(), super_admin(), _create_request())
    assert (exc_info.value.status_code, exc_info.value.code) == (409, "user_exists")
    assert not db.has("insert into public.admin_invites")
    assert db.commits == 0 and db.rollbacks == 1
    assert mailer.sent == []


async def test_a_new_invite_revokes_the_pending_one_for_the_same_email_or_phone(mailer):
    db = create_db()
    await invite_service.create_invite(db.session(), super_admin(), _create_request())
    revoke = db.index_of("set revoked_at = now()")
    assert revoke < db.index_of("insert into public.admin_invites")
    assert "email = %(email)s or phone_number = %(phone)s" in db.statements[revoke]
    assert db.params[revoke] == {"email": "grace@example.com", "phone": "254712000001"}
    assert db.index_of("pg_advisory_xact_lock") == 0


async def test_the_pending_cap_is_enforced(mailer):
    db = create_db(pending=10)
    with pytest.raises(AppError) as exc_info:
        await invite_service.create_invite(db.session(), super_admin(), _create_request())
    assert (exc_info.value.status_code, exc_info.value.code) == (409, "too_many_pending_invites")
    assert not db.has("insert into public.admin_invites")
    assert db.commits == 0
    # Only truly pending invites count toward the cap.
    count_sql = db.statements[db.index_of("select count(*) from public.admin_invites")]
    assert "locked_at is null" in count_sql and "expires_at > now()" in count_sql


async def test_a_failed_send_keeps_the_invite_and_reports_it(api_client, application, monkeypatch):
    db = install(application, super_admin(), create_db())
    mailer = failing_mailer(db)
    monkeypatch.setattr(email_integration, "send_email", mailer.send_email)
    response = await api_client.post("/api/v1/admin/invites", json=INVITE_BODY)
    assert response.status_code == 201
    body = response.json()
    assert body["email_sent"] is False
    assert "resend" in body["message"]
    assert db.commits == 1 and db.rollbacks == 0


async def test_a_concurrent_duplicate_insert_is_409(mailer):
    original = Exception("duplicate key value violates unique constraint")
    original.sqlstate = "23505"

    def unique_violation(db):
        raise IntegrityError("insert into admin_invites", {}, original)

    db = RecordingDB(scalars=[False, 0], results=[FakeResult(), FakeResult(), unique_violation])
    with pytest.raises(AppError) as exc_info:
        await invite_service.create_invite(db.session(), super_admin(), _create_request())
    assert (exc_info.value.status_code, exc_info.value.code) == (409, "invite_conflict")
    assert mailer.sent == []


# Resend --------------------------------------------------------------------------------------


def locked_invite(**overrides):
    row = {
        "id": uuid4(),
        "used_at": None,
        "revoked_at": None,
        "locked_at": None,
        "send_count": 1,
        "inviter_name": "Super Admin",
        "expired": False,
        "in_cooldown": False,
    }
    row.update(overrides)
    return row


async def resend(row):
    db = RecordingDB(results=[rows(row), updated_invite(row["id"])])
    out = await invite_service.resend_invite(db.session(), super_admin(), row["id"])
    return db, out


async def test_resend_replaces_the_code_without_resetting_attempts(monkeypatch):
    row = locked_invite(send_count=2)
    db = RecordingDB(results=[rows(row), updated_invite(row["id"])])
    mailer = FakeMailer(db)
    monkeypatch.setattr(email_integration, "send_email", mailer.send_email)
    old_code = generate_code()
    old_hash = hash_code("invite", row["id"], old_code)

    out = await invite_service.resend_invite(db.session(), super_admin(), row["id"])

    assert out.email_sent is True
    assert "FOR UPDATE" in db.statements[0].upper()
    update_sql = db.statements[1]
    assert "send_count = send_count + 1" in update_sql and "last_sent_at = now()" in update_sql
    assert "attempts" not in update_sql
    new_code = mailer.code()
    stored = db.params[1]["code_hash"]
    assert stored == hash_code("invite", row["id"], new_code)
    assert new_code == old_code or not verify_code("invite", row["id"], old_code, stored)
    assert stored != old_hash or new_code == old_code
    assert mailer.commits_at_send == [1]


@pytest.mark.parametrize(
    "overrides",
    [{"used_at": NOW}, {"revoked_at": NOW}, {"locked_at": NOW}, {"expired": True}],
)
async def test_resend_rejects_an_invite_that_is_not_pending(mailer, overrides):
    with pytest.raises(AppError) as exc_info:
        await resend(locked_invite(**overrides))
    assert (exc_info.value.status_code, exc_info.value.code) == (409, "invite_not_pending")
    assert mailer.sent == []


async def test_resend_respects_the_cooldown(mailer):
    with pytest.raises(AppError) as exc_info:
        await resend(locked_invite(in_cooldown=True))
    assert (exc_info.value.status_code, exc_info.value.code) == (429, "resend_cooldown")
    assert mailer.sent == []


async def test_resend_respects_the_send_cap(mailer):
    with pytest.raises(AppError) as exc_info:
        await resend(locked_invite(send_count=5))
    assert (exc_info.value.status_code, exc_info.value.code) == (409, "send_limit_reached")
    assert mailer.sent == []


async def test_resend_of_an_unknown_invite_is_404(mailer):
    db = RecordingDB(results=[NO_ROW])
    with pytest.raises(AppError) as exc_info:
        await invite_service.resend_invite(db.session(), super_admin(), uuid4())
    assert exc_info.value.code == "invite_not_found"


async def test_resend_route_rejects_a_body(api_client, application):
    db = install(application, super_admin())
    response = await api_client.post(f"/api/v1/admin/invites/{uuid4()}/resend", json={"send_count": 1})
    assert response.status_code == 422
    assert db.statements == []


# List and revoke -----------------------------------------------------------------------------


async def test_invite_list_masks_emails_and_never_shows_a_code_or_hash(api_client, application):
    item = {
        "id": uuid4(),
        "name": "Grace Admin",
        "email": "grace@example.com",
        "status": "pending",
        "created_by_name": "Super Admin",
        "created_at": NOW,
        "expires_at": NOW,
        "send_count": 2,
        "attempts": 3,
        "used_at": None,
        "revoked_at": None,
    }
    db = install(application, super_admin(), RecordingDB(scalars=[1], results=[rows(item)]))
    response = await api_client.get("/api/v1/admin/invites", params={"status": "pending", "limit": 5})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1 and body["limit"] == 5
    out = body["items"][0]
    assert out["email_masked"] == "g***@e***.com" and out["status"] == "pending"
    assert "grace@example.com" not in response.text
    assert "code" not in " ".join(out) and "hash" not in " ".join(out)
    assert "code_hash" not in db.statements[1]
    assert db.params[1]["status"] == "pending"


async def test_invite_list_rejects_an_unknown_status(api_client, application):
    install(application, super_admin())
    response = await api_client.get("/api/v1/admin/invites", params={"status": "accepted"})
    assert response.status_code == 422


async def test_invite_status_is_computed_in_one_statement():
    db = RecordingDB(scalars=[0], results=[rows()])
    await invite_service.list_invites(db.session(), Pagination(), None)
    sql = db.statements[1]
    for status in ("'used'", "'revoked'", "'locked'", "'expired'", "'pending'"):
        assert status in sql
    assert "ORDER BY created_at desc".lower() in sql.lower()


async def test_revoking_a_pending_invite_sets_revoked_at(api_client, application):
    pending = rows({"used_at": None, "revoked_at": None})
    db = install(application, super_admin(), RecordingDB(results=[pending, NO_ROW]))
    response = await api_client.delete(f"/api/v1/admin/invites/{uuid4()}")
    assert response.status_code == 204
    assert db.has("set revoked_at = now()")
    assert db.commits == 1


async def test_revoking_twice_is_idempotent():
    db = RecordingDB(results=[rows({"used_at": None, "revoked_at": NOW})])
    await invite_service.revoke_invite(db.session(), super_admin(), uuid4())
    assert not db.has("set revoked_at = now()")
    assert db.commits == 1


async def test_a_used_invite_cannot_be_revoked():
    db = RecordingDB(results=[rows({"used_at": NOW, "revoked_at": None})])
    with pytest.raises(AppError) as exc_info:
        await invite_service.revoke_invite(db.session(), super_admin(), uuid4())
    assert (exc_info.value.status_code, exc_info.value.code) == (409, "invite_not_pending")


# Accept --------------------------------------------------------------------------------------


def claimed_invite(created_by):
    return rows(
        {
            "name": "Grace Admin",
            "phone_number": "254712000001",
            "email": "grace@example.com",
            "created_by": created_by,
        }
    )


def inserted_user(db):
    params = db.params[-1]
    return rows(
        {
            "id": uuid4(),
            "name": params["name"],
            "phone_number": params["phone"],
            "email": params["email"],
            "role": "admin",
            "must_change_password": False,
            "created_at": NOW,
        }
    )


def accept_body(code, password=STRONG_PASSWORD, email="grace@example.com"):
    return {"email": email, "code": code, "password": password}


async def test_a_correct_code_creates_an_admin_who_can_log_in(api_client, application, caplog):
    caplog.set_level(logging.DEBUG)
    code = generate_code()
    inviter = uuid4()
    db = install(application, None, RecordingDB(results=[invite_attempt(code), claimed_invite(inviter), inserted_user]))

    response = await api_client.post("/api/v1/auth/accept-invite", json=accept_body(format_code(code).lower()))

    assert response.status_code == 201, response.text
    body = response.json()
    assert (body["role"], body["must_change_password"]) == ("admin", False)
    assert STRONG_PASSWORD not in response.text and "password_hash" not in response.text
    insert_sql = db.statements[db.index_of("insert into public.users")]
    assert "'admin', false, false" in insert_sql and "password_changed_at" in insert_sql
    stored = db.params_for("insert into public.users")
    assert stored["created_by"] == inviter
    assert verify_password(stored["password_hash"], STRONG_PASSWORD)
    assert db.commits == 2  # step A, then step B
    assert code not in caplog.text and STRONG_PASSWORD not in caplog.text

    admin = User(
        id=uuid4(),
        name="Grace Admin",
        phone_number="254712000001",
        email="grace@example.com",
        password_hash=stored["password_hash"],
        role=UserRole.ADMIN,
        is_super_admin=False,
        must_change_password=False,
        is_disabled=False,
    )
    auth_service.get_login_throttle().reset()
    login = await auth_service.login(FakeDB(scalars=[admin]).session(), "grace@example.com", STRONG_PASSWORD)
    assert login["role"] == "admin" and login["must_change_password"] is False


def step_a_sql(db):
    return db.statements[0]


async def test_step_a_only_counts_attempts_on_a_usable_invite():
    db = RecordingDB(results=[NO_ROW])
    with pytest.raises(AppError):
        await invite_service.accept_invite(db.session(), AcceptInviteRequest(**accept_body("ABCD-EFGH")))
    sql = step_a_sql(db)
    assert "set attempts = attempts + 1" in sql
    for condition in (
        "used_at is null",
        "revoked_at is null",
        "locked_at is null",
        "expires_at > now()",
        "code_expires_at > now()",
        "attempts < %(max_attempts)s",
    ):
        assert condition in sql
    assert db.params[0]["max_attempts"] == 10
    assert db.commits == 1


def unique_violation(db):
    original = Exception("duplicate key value violates unique constraint")
    original.sqlstate = "23505"
    raise IntegrityError("insert into users", {}, original)


def failure_scripts():
    """One script per failure type; each returns (results queue, code sent by the client)."""
    code = generate_code()
    other = "ABCDEFGH" if code != "ABCDEFGH" else "HGFEDCBA"
    return {
        # Unknown email, expired code, expired invite, used, revoked and locked all leave step A
        # without a row because of its WHERE clause.
        "no_usable_invite": ([NO_ROW], code),
        "wrong_code": ([invite_attempt(code)], other),
        "claimed_meanwhile": ([invite_attempt(code), NO_ROW], code),
        "unique_conflict": ([invite_attempt(code), claimed_invite(uuid4()), unique_violation], code),
    }


async def test_every_accept_failure_returns_the_identical_400(api_client, application):
    bodies = []
    for name, (results, code) in failure_scripts().items():
        limiter._storage.reset()
        db = install(application, None, RecordingDB(results=results))
        response = await api_client.post("/api/v1/auth/accept-invite", json=accept_body(code))
        assert response.status_code == 400, name
        bodies.append(without_request_id(response.json()))
        assert db.commits >= 1, name  # the attempt is always committed
    assert bodies == [GENERIC_FAILURE] * len(bodies)


async def test_a_wrong_code_still_commits_the_attempt():
    code = generate_code()
    db = RecordingDB(results=[invite_attempt(code, attempts=4)])
    with pytest.raises(AppError) as exc_info:
        await invite_service.accept_invite(db.session(), AcceptInviteRequest(**accept_body("ZZZZ-ZZZZ")))
    assert exc_info.value.code == "invalid_or_expired_code"
    assert db.commits == 1 and db.rollbacks == 0
    assert not db.has("set locked_at")


async def test_the_tenth_wrong_attempt_locks_the_invite():
    code = generate_code()
    db = RecordingDB(results=[invite_attempt(code, attempts=10), NO_ROW])
    with pytest.raises(AppError):
        await invite_service.accept_invite(db.session(), AcceptInviteRequest(**accept_body("ZZZZ-ZZZZ")))
    lock = db.index_of("set locked_at = now()")
    assert lock == 1
    assert db.commits == 1


async def test_a_correct_code_on_the_last_attempt_still_works():
    code = generate_code()
    db = RecordingDB(results=[invite_attempt(code, attempts=10), claimed_invite(uuid4()), inserted_user])
    user = await invite_service.accept_invite(db.session(), AcceptInviteRequest(**accept_body(code)))
    assert user["role"] == "admin"
    assert not db.has("set locked_at")


async def test_a_unique_violation_rolls_back_the_claim():
    code = generate_code()
    db = RecordingDB(results=[invite_attempt(code), claimed_invite(uuid4()), unique_violation])
    with pytest.raises(AppError) as exc_info:
        await invite_service.accept_invite(db.session(), AcceptInviteRequest(**accept_body(code)))
    assert exc_info.value.code == "invalid_or_expired_code"
    assert db.commits == 1  # only step A; the claim was rolled back with the insert
    assert db.rollbacks == 1


@pytest.mark.parametrize(
    ("password", "reason"),
    [
        ("Short-pass1", "Use at least 12 characters."),
        ("grace@example.com", "The password must not be your email, name or phone number."),
        ("password1234", "This password is too common."),
    ],
)
async def test_accept_checks_the_admin_password_policy_before_the_code(api_client, application, password, reason):
    db = install(application, None)
    response = await api_client.post("/api/v1/auth/accept-invite", json=accept_body("ABCD-EFGH", password))
    assert response.status_code == 422
    assert without_request_id(response.json())["error"] == {
        "code": "password_too_weak",
        "message": "The password is too weak.",
        "reason": reason,
    }
    assert db.statements == []


async def test_accept_rejects_the_invited_name_or_phone_as_password_after_the_code():
    code = generate_code()
    db = RecordingDB(results=[invite_attempt(code)])
    with pytest.raises(AppError) as exc_info:
        await invite_service.accept_invite(
            db.session(), AcceptInviteRequest(**accept_body(code, password="254712000001"))
        )
    assert exc_info.value.code == "password_too_weak"
    assert not db.has("set used_at")


@pytest.mark.parametrize(
    "body",
    [
        {"email": "grace@example.com", "code": "ABC", "password": STRONG_PASSWORD},
        {"email": "grace@example.com", "code": "A" * 21, "password": STRONG_PASSWORD},
        {"email": "grace@example.com", "code": "ABCD-EFGH", "password": STRONG_PASSWORD, "role": "admin"},
        {"email": "grace@example.com", "code": "ABCD-EFGH", "password": STRONG_PASSWORD, "is_super_admin": True},
    ],
)
async def test_accept_bodies_are_validated(api_client, application, body):
    db = install(application, None)
    response = await api_client.post("/api/v1/auth/accept-invite", json=body)
    assert response.status_code == 422
    assert db.statements == []


async def test_codes_unavailable_without_a_pepper(api_client, application, monkeypatch):
    monkeypatch.setattr(get_settings(), "invite_code_pepper", "")
    install(application, super_admin())
    responses = [
        await api_client.post("/api/v1/admin/invites", json=INVITE_BODY),
        await api_client.post(f"/api/v1/admin/invites/{uuid4()}/resend"),
        await api_client.post("/api/v1/auth/accept-invite", json=accept_body("ABCD-EFGH")),
        await api_client.post("/api/v1/auth/invite/request-code", json={"email": "grace@example.com"}),
        await api_client.post("/api/v1/auth/password-reset/request", json={"email": "grace@example.com"}),
        await api_client.post(
            "/api/v1/auth/password-reset/confirm",
            json={"email": "grace@example.com", "code": "ABCD-EFGH", "new_password": STRONG_PASSWORD},
        ),
    ]
    assert {(r.status_code, r.json()["error"]["code"]) for r in responses} == {(503, "codes_unavailable")}
    health = await api_client.get("/api/v1/health")
    assert health.status_code == 200


# Request-code --------------------------------------------------------------------------------

REQUEST_CODE_BODY = {"message": "If an invitation exists for this address, a new code has been sent."}


async def test_request_code_answers_the_same_for_existing_and_unknown_emails(api_client, application, mailer):
    invite_id = uuid4()
    existing = RecordingDB(results=[rows({"id": invite_id, "inviter_name": "Super Admin"}), updated_invite(invite_id)])
    install(application, None, existing)
    first = await api_client.post("/api/v1/auth/invite/request-code", json={"email": "Grace@Example.com"})
    install(application, None, RecordingDB(results=[NO_ROW]))
    second = await api_client.post("/api/v1/auth/invite/request-code", json={"email": "nobody@example.com"})
    assert (first.status_code, first.json()) == (202, REQUEST_CODE_BODY)
    assert (second.status_code, second.json()) == (202, REQUEST_CODE_BODY)
    assert len(mailer.sent) == 1 and mailer.sent[0].to == "grace@example.com"


async def test_request_code_sends_in_a_background_task_and_keeps_attempts(mailer):
    invite_id = uuid4()
    db = RecordingDB(results=[rows({"id": invite_id, "inviter_name": "Super Admin"}), updated_invite(invite_id)])
    tasks = BackgroundTasks()
    await invite_service.request_invite_code(db.session(), "grace@example.com", tasks)
    assert mailer.sent == []  # nothing is sent before the response
    assert len(tasks.tasks) == 1
    assert db.commits == 1
    select_sql = db.statements[0]
    for condition in ("send_count < %(max_sends)s", "last_sent_at <= now() -", "locked_at is null", "FOR UPDATE"):
        assert condition.lower() in select_sql.lower()
    assert "attempts" not in db.statements[1]
    await tasks()
    code = mailer.code()
    assert db.params[1]["code_hash"] == hash_code("invite", invite_id, code)


async def test_request_code_for_an_unknown_email_schedules_nothing(mailer):
    db = RecordingDB(results=[NO_ROW])
    tasks = BackgroundTasks()
    await invite_service.request_invite_code(db.session(), "nobody@example.com", tasks)
    assert tasks.tasks == []
    assert db.commits == 1 and len(db.statements) == 1
