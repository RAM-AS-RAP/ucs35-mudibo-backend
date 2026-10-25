"""Fakes shared by the invite, password reset and email tests.

``RecordingDB`` is the scripted ``FakeDB`` from ``payment_fakes`` that also keeps the parameters of
every statement, so a test can check what would have been stored (for example that only an HMAC of
the code is written). ``FakeMailer`` replaces ``app.integrations.email.send_email`` and records the
messages, the number of commits and the open transactions at the moment each one was sent.
"""

import re
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from payment_fakes import NOW, FakeDB, FakeResult, FakeSession

from app.core.codes import hash_code
from app.core.dependencies import get_current_user
from app.db import get_db
from app.integrations.email import EmailDeliveryError
from app.models.enums import UserRole

PEPPER = "test-only-invite-code-pepper-with-at-least-32-characters"
FRONTEND = "https://admin.agrisence.test"
CODE_IN_TEXT = re.compile(r"\b([A-HJ-NP-Z2-9]{4})-([A-HJ-NP-Z2-9]{4})\b")


def configure_codes(monkeypatch, settings):
    """Give the cached settings a pepper and a frontend URL, and keep email on the console provider."""
    monkeypatch.setattr(settings, "invite_code_pepper", PEPPER)
    monkeypatch.setattr(settings, "frontend_base_url", FRONTEND)
    monkeypatch.setattr(settings, "email_provider", "console")
    return settings


class RecordingSession(FakeSession):
    async def scalar(self, statement, params=None):
        self.db.params.append(dict(params or {}))
        return await super().scalar(statement, params)

    async def scalars(self, statement):
        self.db.params.append({})
        return await super().scalars(statement)

    async def execute(self, statement, params=None):
        self.db.params.append(dict(params or {}))
        return await super().execute(statement, params)


class RecordingDB(FakeDB):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.params: list[dict] = []

    def session(self) -> RecordingSession:
        session = RecordingSession(self)
        self.sessions.append(session)
        return session

    def index_of(self, fragment: str) -> int:
        return next(i for i, sql in enumerate(self.statements) if fragment in sql)

    def has(self, fragment: str) -> bool:
        return any(fragment in sql for sql in self.statements)

    def params_for(self, fragment: str) -> dict:
        return self.params[self.index_of(fragment)]


class FakeMailer:
    def __init__(self, db: FakeDB | None = None, error: Exception | None = None):
        self.db = db
        self.error = error
        self.sent: list = []
        self.commits_at_send: list[int] = []
        self.open_at_send: list[list] = []

    async def send_email(self, message, **kwargs):
        del kwargs
        if self.db is not None:
            self.commits_at_send.append(self.db.commits)
            self.open_at_send.append(self.db.open_transactions())
        self.sent.append(message)
        if self.error is not None:
            raise self.error

    def code(self, index: int = -1) -> str:
        match = CODE_IN_TEXT.search(self.sent[index].text)
        assert match is not None, "no code in the email"
        return match.group(1) + match.group(2)


def failing_mailer(db=None, category="connection_timeout") -> FakeMailer:
    return FakeMailer(db, error=EmailDeliveryError(category))


def person(role=UserRole.ADMIN, *, super_admin=False, **overrides):
    values = {
        "id": uuid4(),
        "name": "Super Admin" if super_admin else f"Test {role.value}",
        "email": "super@example.com" if super_admin else f"{role.value}@example.com",
        "email_verified_at": NOW,
        "role": role,
        "is_super_admin": super_admin,
        "is_disabled": False,
        "must_change_password": False,
        "password_changed_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def install(application, user, db: FakeDB | None = None) -> FakeDB:
    """Override the authenticated user (None keeps the real bearer check) and the request session."""
    if user is not None:
        application.dependency_overrides[get_current_user] = lambda: user
    session_db = db or RecordingDB()

    async def request_session():
        yield session_db.session()

    application.dependency_overrides[get_db] = request_session
    return session_db


def inserted_invite(db: "RecordingDB") -> FakeResult:
    """The ``returning`` row of the invite insert, built from the statement's parameters."""
    params = db.params[-1]
    return FakeResult(
        mapping_rows=[
            {
                "id": params["id"],
                "name": params["name"],
                "email": params["email"],
                "expires_at": NOW + timedelta(days=7),
                "code_expires_at": NOW + timedelta(minutes=15),
            }
        ]
    )


def updated_invite(invite_id, email="grace@example.com"):
    """The ``returning`` row of a code replacement (resend or request-code)."""

    def result(db) -> FakeResult:
        del db
        return FakeResult(
            mapping_rows=[
                {
                    "id": invite_id,
                    "name": "Grace Admin",
                    "email": email,
                    "expires_at": NOW + timedelta(days=7),
                    "code_expires_at": NOW + timedelta(minutes=15),
                }
            ]
        )

    return result


def invite_attempt(code: str, *, attempts=1, invite_id=None) -> FakeResult:
    """Step A's ``returning`` row for an invite whose stored hash matches ``code``."""
    invite_id = invite_id or uuid4()
    return FakeResult(
        mapping_rows=[
            {
                "id": invite_id,
                "name": "Grace Admin",
                "code_hash": hash_code("invite", invite_id, code),
                "attempts": attempts,
            }
        ]
    )


def reset_attempt(code: str, *, attempts=1, role="farmer", user_id=None, code_id=None) -> FakeResult:
    """Step A's ``returning`` row for a reset code whose stored hash matches ``code``."""
    code_id = code_id or uuid4()
    return FakeResult(
        mapping_rows=[
            {
                "id": code_id,
                "user_id": user_id or uuid4(),
                "code_hash": hash_code("reset", code_id, code),
                "attempts": attempts,
                "role": role,
                "name": "Amina Farmer",
                "email": "amina@example.com",
            }
        ]
    )


def rows(*mapping_rows) -> FakeResult:
    return FakeResult(mapping_rows=list(mapping_rows))


NO_ROW = FakeResult()
