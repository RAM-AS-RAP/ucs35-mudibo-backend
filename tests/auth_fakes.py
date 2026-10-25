"""An in-memory stand-in for the database behind registration and sign in.

``AuthStore`` keeps ``users``, ``pending_registrations`` and ``login_challenges`` in dictionaries and
answers the services' SQL by recognising the statement constants of ``registration_service`` and
``auth_service`` (by identity, so the SQL text is never parsed), plus ORM ``select(User)`` lookups by
id or email. It follows the database rules the services rely on: the database clock (``store.now``,
moved with ``advance``), the one-open-row unique indexes, the unique email, the immutable used rows,
and transactions (``commit`` keeps changes, ``rollback`` and closing a session undo uncommitted ones).

Tests drive whole flows through the API with it: register, read the code from the fake mailer, verify,
then call ``/auth/me`` with the returned token.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

from payment_fakes import NOW, FakeResult
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from app.core.security import hash_password
from app.db import get_db
from app.models.enums import UserRole
from app.services import auth_service as auth
from app.services import registration_service as reg


def unique_violation(what: str) -> IntegrityError:
    original = Exception(f"duplicate key value violates unique constraint ({what})")
    original.sqlstate = "23505"
    return IntegrityError("fake statement", {}, original)


class AuthStore:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now
        self.users: dict[UUID, SimpleNamespace] = {}
        self.pending: dict[UUID, dict[str, Any]] = {}
        self.challenges: dict[UUID, dict[str, Any]] = {}
        self.sessions: list[AuthSession] = []
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.commits = 0
        self.rollbacks = 0
        self.locks: list[str] = []
        # Called before a user insert; a test can create a conflicting user there.
        self.before_user_insert: Callable[[AuthStore], None] | None = None

    # Helpers for tests ---------------------------------------------------------------------------

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)

    def add_user(
        self,
        role: UserRole = UserRole.FARMER,
        *,
        email: str | None = None,
        password: str = "Correct-Horse-9!",
        password_hash: str | None = None,
        **overrides: Any,
    ) -> SimpleNamespace:
        user_id = overrides.pop("id", uuid4())
        values = {
            "id": user_id,
            "name": f"Test {role.value}",
            "email": (email or f"{role.value}-{user_id.hex[:8]}@example.com").lower(),
            "email_verified_at": self.now,
            "password_hash": password_hash or hash_password(password),
            "role": role,
            "is_super_admin": False,
            "must_change_password": False,
            "is_disabled": False,
            "disabled_at": None,
            "password_changed_at": None,
            "created_by": None,
            "created_at": self.now,
        }
        values.update(overrides)
        user = SimpleNamespace(**values)
        self.users[user.id] = user
        return user

    def user_by_email(self, email: str) -> SimpleNamespace | None:
        return next((user for user in self.users.values() if user.email.lower() == email.lower()), None)

    def open_pending(self, email: str) -> list[dict[str, Any]]:
        return [row for row in self.pending.values() if row["email"] == email and row["used_at"] is None]

    def active_challenges(self, user_id: UUID) -> list[dict[str, Any]]:
        return [row for row in self.challenges.values() if row["user_id"] == user_id and row["used_at"] is None]

    def statement_names(self) -> list[str]:
        return [name for name, _ in self.executed]

    def session(self) -> AuthSession:
        session = AuthSession(self)
        self.sessions.append(session)
        return session

    def open_transactions(self) -> list[AuthSession]:
        return [session for session in self.sessions if session.in_transaction]

    def install(self, application) -> AuthStore:
        """Use this store for the request session of ``application``."""

        async def request_session():
            session = self.session()
            try:
                yield session
            finally:
                await session.close()

        application.dependency_overrides[get_db] = request_session
        return self


def _seconds(value: timedelta) -> int:
    return math.ceil(value.total_seconds())


class AuthSession:
    def __init__(self, store: AuthStore) -> None:
        self.store = store
        self.undo: list[Callable[[], None]] = []
        self.in_transaction = False
        self.handlers: dict[int, Callable[[dict[str, Any]], FakeResult]] = {
            id(reg.SQL_ACCOUNT_EXISTS): self._account_exists,
            id(reg.SQL_LOCK_EMAIL): self._lock,
            id(reg.SQL_FIND_OPEN): self._find_open,
            id(reg.SQL_UPDATE_KEEP_CODE): self._update_keep_code,
            id(reg.SQL_UPDATE_NEW_CODE): self._update_new_code,
            id(reg.SQL_SUPERSEDE): self._supersede_pending,
            id(reg.SQL_INSERT_PENDING): self._insert_pending,
            id(reg.SQL_COUNT_ATTEMPT): self._count_pending_attempt,
            id(reg.SQL_LOCK_ROW): self._lock_pending,
            id(reg.SQL_CLAIM): self._claim_pending,
            id(reg.SQL_INSERT_USER): self._insert_user,
            id(reg.SQL_FIND_FOR_RESEND): self._find_pending_for_resend,
            id(reg.SQL_STORE_CODE): self._store_pending_code,
            id(auth.SQL_LOCK_USER_CHALLENGES): self._lock,
            id(auth.SQL_RECENT_CHALLENGES): self._recent_challenges,
            id(auth.SQL_SUPERSEDE_CHALLENGES): self._supersede_challenges,
            id(auth.SQL_INSERT_CHALLENGE): self._insert_challenge,
            id(auth.SQL_COUNT_LOGIN_ATTEMPT): self._count_login_attempt,
            id(auth.SQL_LOCK_CHALLENGE): self._lock_challenge,
            id(auth.SQL_CLAIM_CHALLENGE): self._claim_challenge,
            id(auth.SQL_FIND_CHALLENGE_FOR_RESEND): self._find_challenge_for_resend,
            id(auth.SQL_STORE_LOGIN_CODE): self._store_login_code,
        }
        self.names = {id(value): name for module in (reg, auth) for name, value in vars(module).items()}

    # Session protocol ----------------------------------------------------------------------------

    async def execute(self, statement, params=None) -> FakeResult:
        handler = self.handlers.get(id(statement))
        if handler is None:
            raise AssertionError(f"Unscripted statement: {statement}")
        self.in_transaction = True
        values = dict(params or {})
        self.store.executed.append((self.names.get(id(statement), "?"), values))
        return handler(values)

    async def scalar(self, statement, params=None):
        del params
        self.in_transaction = True
        compiled = statement.compile(dialect=postgresql.dialect())
        values = list(compiled.params.values())
        self.store.executed.append(("select_user", {"values": values}))
        for value in values:
            if isinstance(value, UUID):
                return self.store.users.get(value)
            if isinstance(value, str):
                return self.store.user_by_email(value)
        raise AssertionError(f"Unexpected ORM statement: {compiled}")

    async def commit(self) -> None:
        self.undo.clear()
        self.in_transaction = False
        self.store.commits += 1

    async def rollback(self) -> None:
        self._undo()
        self.store.rollbacks += 1

    async def close(self) -> None:
        self._undo()

    def _undo(self) -> None:
        for step in reversed(self.undo):
            step()
        self.undo.clear()
        self.in_transaction = False

    # Mutation helpers ----------------------------------------------------------------------------

    def _change(self, row: dict[str, Any], **changes: Any) -> None:
        if row.get("used_at") is not None:
            raise AssertionError("trigger: a used row cannot be changed")
        if "attempts" in changes and changes["attempts"] < row["attempts"]:
            raise AssertionError("trigger: attempts can only increase")
        if "send_count" in changes and not 1 <= changes["send_count"] <= 5:
            raise AssertionError("check: send_count between 1 and 5")
        before = dict(row)
        row.update(changes)
        self.undo.append(lambda: (row.clear(), row.update(before)))

    def _insert(self, table: dict[UUID, Any], key: UUID, row: Any) -> None:
        table[key] = row
        self.undo.append(lambda: table.pop(key, None))

    @staticmethod
    def _one(**values: Any) -> FakeResult:
        return FakeResult(mapping_rows=[values])

    # Shared ----------------------------------------------------------------------------------------

    def _lock(self, params: dict[str, Any]) -> FakeResult:
        self.store.locks.append(params["key"])
        return FakeResult()

    # Registration --------------------------------------------------------------------------------

    def _account_exists(self, params: dict[str, Any]) -> FakeResult:
        return self._one(account_exists=self.store.user_by_email(params["email"]) is not None, db_now=self.store.now)

    def _open_row(self, email: str) -> dict[str, Any] | None:
        now = self.store.now
        for row in self.store.pending.values():
            if (
                row["email"] == email
                and row["used_at"] is None
                and row["locked_at"] is None
                and row["expires_at"] > now
            ):
                return row
        return None

    def _find_open(self, params: dict[str, Any]) -> FakeResult:
        row = self._open_row(params["email"])
        if row is None:
            return FakeResult()
        now = self.store.now
        cooldown = timedelta(seconds=params["cooldown"])
        return self._one(
            id=row["id"],
            role=row["role"],
            send_count=row["send_count"],
            code_expires_at=row["code_expires_at"],
            code_valid=row["code_expires_at"] > now,
            in_cooldown=row["last_sent_at"] > now - cooldown,
            wait_seconds=max(0, _seconds(row["last_sent_at"] + cooldown - now)),
        )

    def _check_token_unique(self, token_hash: str, own_id: UUID | None = None) -> None:
        for table in (self.store.pending, self.store.challenges):
            for key, row in table.items():
                if row["token_hash"] == token_hash and key != own_id:
                    raise unique_violation("token_hash")

    def _update_keep_code(self, params: dict[str, Any]) -> FakeResult:
        row = self.store.pending[params["id"]]
        self._check_token_unique(params["token_hash"], row["id"])
        self._change(
            row,
            name=params["name"],
            password_hash=params["password_hash"],
            token_hash=params["token_hash"],
            expires_at=self.store.now + timedelta(minutes=params["ttl_minutes"]),
        )
        return self._one(code_expires_at=row["code_expires_at"])

    def _update_new_code(self, params: dict[str, Any]) -> FakeResult:
        row = self.store.pending[params["id"]]
        self._check_token_unique(params["token_hash"], row["id"])
        now = self.store.now
        self._change(
            row,
            name=params["name"],
            password_hash=params["password_hash"],
            token_hash=params["token_hash"],
            expires_at=now + timedelta(minutes=params["ttl_minutes"]),
            code_hash=params["code_hash"],
            code_expires_at=now + timedelta(minutes=params["code_minutes"]),
            last_sent_at=now,
            send_count=row["send_count"] + 1,
        )
        return self._one(code_expires_at=row["code_expires_at"])

    def _supersede_pending(self, params: dict[str, Any]) -> FakeResult:
        for row in list(self.store.pending.values()):
            if row["email"] == params["email"] and row["used_at"] is None:
                self._change(row, used_at=self.store.now)
        return FakeResult()

    def _insert_pending(self, params: dict[str, Any]) -> FakeResult:
        if params["email"] != params["email"].lower():
            raise AssertionError("check: email must be lower case")
        if params["role"] not in ("farmer", "buyer"):
            raise AssertionError("check: role must be farmer or buyer")
        if self.store.open_pending(params["email"]):
            raise unique_violation("pending_registrations_one_open_email_idx")
        self._check_token_unique(params["token_hash"])
        now = self.store.now
        row = {
            "id": params["id"],
            "email": params["email"],
            "name": params["name"],
            "role": params["role"],
            "password_hash": params["password_hash"],
            "token_hash": params["token_hash"],
            "code_hash": params["code_hash"],
            "code_expires_at": now + timedelta(minutes=params["code_minutes"]),
            "last_sent_at": now,
            "send_count": 1,
            "attempts": 0,
            "locked_at": None,
            "created_at": now,
            "expires_at": now + timedelta(minutes=params["ttl_minutes"]),
            "used_at": None,
        }
        self._insert(self.store.pending, row["id"], row)
        return self._one(code_expires_at=row["code_expires_at"])

    def _count_pending_attempt(self, params: dict[str, Any]) -> FakeResult:
        now = self.store.now
        for row in self.store.pending.values():
            if (
                row["token_hash"] == params["token_hash"]
                and row["used_at"] is None
                and row["locked_at"] is None
                and row["expires_at"] > now
                and row["code_expires_at"] > now
                and row["attempts"] < params["max_attempts"]
            ):
                self._change(row, attempts=row["attempts"] + 1)
                return self._one(id=row["id"], code_hash=row["code_hash"], attempts=row["attempts"])
        return FakeResult()

    def _lock_pending(self, params: dict[str, Any]) -> FakeResult:
        row = self.store.pending[params["id"]]
        if row["locked_at"] is None:
            self._change(row, locked_at=self.store.now)
        return FakeResult()

    def _claim_pending(self, params: dict[str, Any]) -> FakeResult:
        row = self.store.pending.get(params["id"])
        now = self.store.now
        if row is None or row["used_at"] is not None or row["locked_at"] is not None or row["expires_at"] <= now:
            return FakeResult()
        self._change(row, used_at=now)
        return self._one(email=row["email"], name=row["name"], role=row["role"], password_hash=row["password_hash"])

    def _insert_user(self, params: dict[str, Any]) -> FakeResult:
        if self.store.before_user_insert is not None:
            hook, self.store.before_user_insert = self.store.before_user_insert, None
            hook(self.store)
        if self.store.user_by_email(params["email"]) is not None:
            raise unique_violation("users_email_lower_unique_idx")
        now = self.store.now
        user = SimpleNamespace(
            id=uuid4(),
            name=params["name"],
            email=params["email"],
            email_verified_at=now,
            password_hash=params["password_hash"],
            role=UserRole(params["role"]),
            is_super_admin=False,
            must_change_password=False,
            is_disabled=False,
            disabled_at=None,
            password_changed_at=now,
            created_by=None,
            created_at=now,
        )
        self._insert(self.store.users, user.id, user)
        return self._one(id=user.id, role=user.role.value, is_super_admin=False, must_change_password=False)

    def _find_pending_for_resend(self, params: dict[str, Any]) -> FakeResult:
        now = self.store.now
        cooldown = timedelta(seconds=params["cooldown"])
        for row in self.store.pending.values():
            if (
                row["token_hash"] == params["token_hash"]
                and row["used_at"] is None
                and row["locked_at"] is None
                and row["expires_at"] > now
                and row["send_count"] < params["max_sends"]
                and row["last_sent_at"] <= now - cooldown
            ):
                return self._one(id=row["id"], email=row["email"])
        return FakeResult()

    def _store_pending_code(self, params: dict[str, Any]) -> FakeResult:
        row = self.store.pending[params["id"]]
        now = self.store.now
        self._change(
            row,
            code_hash=params["code_hash"],
            code_expires_at=now + timedelta(minutes=params["code_minutes"]),
            last_sent_at=now,
            send_count=row["send_count"] + 1,
        )
        return FakeResult()

    # Login challenges ----------------------------------------------------------------------------

    def _recent_challenges(self, params: dict[str, Any]) -> FakeResult:
        now = self.store.now
        recent = [
            row["created_at"]
            for row in self.store.challenges.values()
            if row["user_id"] == params["user_id"] and row["created_at"] > now - timedelta(hours=1)
        ]
        retry_after = _seconds(min(recent) + timedelta(hours=1) - now) if recent else None
        return self._one(recent=len(recent), retry_after=retry_after)

    def _supersede_challenges(self, params: dict[str, Any]) -> FakeResult:
        for row in list(self.store.challenges.values()):
            if row["user_id"] == params["user_id"] and row["used_at"] is None:
                self._change(row, used_at=self.store.now)
        return FakeResult()

    def _insert_challenge(self, params: dict[str, Any]) -> FakeResult:
        if self.store.active_challenges(params["user_id"]):
            raise unique_violation("login_challenges_one_active_user_idx")
        self._check_token_unique(params["token_hash"])
        now = self.store.now
        row = {
            "id": params["id"],
            "user_id": params["user_id"],
            "token_hash": params["token_hash"],
            "code_hash": params["code_hash"],
            "code_expires_at": now + timedelta(minutes=params["code_minutes"]),
            "last_sent_at": now,
            "send_count": 1,
            "attempts": 0,
            "locked_at": None,
            "created_at": now,
            "expires_at": now + timedelta(minutes=params["challenge_minutes"]),
            "used_at": None,
        }
        self._insert(self.store.challenges, row["id"], row)
        return self._one(code_expires_at=row["code_expires_at"])

    def _count_login_attempt(self, params: dict[str, Any]) -> FakeResult:
        now = self.store.now
        for row in self.store.challenges.values():
            if (
                row["token_hash"] == params["token_hash"]
                and row["used_at"] is None
                and row["locked_at"] is None
                and row["expires_at"] > now
                and row["code_expires_at"] > now
                and row["attempts"] < params["max_attempts"]
            ):
                self._change(row, attempts=row["attempts"] + 1)
                return self._one(
                    id=row["id"], user_id=row["user_id"], code_hash=row["code_hash"], attempts=row["attempts"]
                )
        return FakeResult()

    def _lock_challenge(self, params: dict[str, Any]) -> FakeResult:
        row = self.store.challenges[params["id"]]
        if row["locked_at"] is None:
            self._change(row, locked_at=self.store.now)
        return FakeResult()

    def _claim_challenge(self, params: dict[str, Any]) -> FakeResult:
        row = self.store.challenges.get(params["id"])
        now = self.store.now
        if row is None or row["used_at"] is not None or row["locked_at"] is not None or row["expires_at"] <= now:
            return FakeResult()
        self._change(row, used_at=now)
        return self._one(user_id=row["user_id"])

    def _find_challenge_for_resend(self, params: dict[str, Any]) -> FakeResult:
        now = self.store.now
        cooldown = timedelta(seconds=params["cooldown"])
        for row in self.store.challenges.values():
            user = self.store.users.get(row["user_id"])
            if (
                row["token_hash"] == params["token_hash"]
                and row["used_at"] is None
                and row["locked_at"] is None
                and row["expires_at"] > now
                and user is not None
                and not user.is_disabled
            ):
                return self._one(
                    id=row["id"],
                    user_id=row["user_id"],
                    send_count=row["send_count"],
                    email=user.email,
                    wait_seconds=max(0, _seconds(row["last_sent_at"] + cooldown - now)),
                )
        return FakeResult()

    def _store_login_code(self, params: dict[str, Any]) -> FakeResult:
        row = self.store.challenges[params["id"]]
        now = self.store.now
        self._change(
            row,
            code_hash=params["code_hash"],
            code_expires_at=min(now + timedelta(minutes=params["code_minutes"]), row["expires_at"]),
            last_sent_at=now,
            send_count=row["send_count"] + 1,
        )
        return self._one(code_expires_at=row["code_expires_at"])
