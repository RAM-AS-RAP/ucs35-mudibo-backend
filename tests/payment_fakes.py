"""Fakes shared by the payment tests: a scripted database, a fake Daraja client, settings and rows.

Every session handed out by one ``FakeDB`` (the request session and the ones from its session
factory) reads scripted values from the same queues, in statement order. ``log_event`` sessions only
add and commit, so they never consume scripted values.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from app.core.config import Settings
from app.integrations.daraja import StkPushResult, StkQueryResult
from app.models.enums import OrderStatus, PaymentStatus, UserRole
from app.models.order import Order
from app.models.payment import Payment, PaymentEvent

NOW = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
BUYER_PHONE = "254712345678"
TEST_MSISDN = "254708374149"
CONSUMER_KEY = "CK-recognisable-consumer-key-8f3a"
CONSUMER_SECRET = "CS-recognisable-consumer-secret-91be"
PASSKEY = "PK-recognisable-passkey-c0ffee"
CALLBACK_TOKEN = "CBT_recognisable_callback_token_5d2e"
SECRETS = (CONSUMER_KEY, CONSUMER_SECRET, PASSKEY, CALLBACK_TOKEN)


def payment_settings(**overrides) -> Settings:
    values = {
        "mpesa_consumer_key": CONSUMER_KEY,
        "mpesa_consumer_secret": CONSUMER_SECRET,
        "mpesa_passkey": PASSKEY,
        "mpesa_shortcode": "174379",
        "mpesa_callback_base_url": "https://payments.example.test",
        "mpesa_callback_token": CALLBACK_TOKEN,
        "mpesa_stk_target": "entered",
        "mpesa_test_msisdn": TEST_MSISDN,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def use_settings(monkeypatch, settings: Settings) -> Settings:
    """Point every module that reads payment settings at ``settings``."""
    from app.routers import payments as payments_router
    from app.services import order_service, payment_service

    for module in (payment_service, order_service, payments_router):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    return settings


def compiled(statement) -> str:
    if hasattr(statement, "compile"):
        return str(statement.compile(dialect=postgresql.dialect()))
    return str(statement)


def bound_values(statement) -> list[object]:
    """The bound parameter values of a recorded statement."""
    return list(statement.compile(dialect=postgresql.dialect()).params.values())


class FakeResult:
    def __init__(self, rows=None, mapping_rows=None):
        self.rows = list(rows or [])
        self.mapping_rows = list(mapping_rows or [])

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows

    def mappings(self):
        return SimpleNamespace(
            first=lambda: self.mapping_rows[0] if self.mapping_rows else None,
            all=lambda: self.mapping_rows,
            one=lambda: self.mapping_rows[0],
        )


class FakeScalars:
    def __init__(self, items):
        self.items = list(items)

    def all(self):
        return self.items

    def __iter__(self):
        return iter(self.items)


class FakeDB:
    """Scripted database shared by all sessions it creates.

    Queue items may be callables taking the ``FakeDB``; they are called when popped, which lets a
    script return a row that the code under test inserted earlier.
    """

    def __init__(self, scalars=(), results=(), lists=(), commit_errors=()):
        self.scalar_queue = list(scalars)
        self.result_queue = list(results)
        self.list_queue = list(lists)
        self.commit_errors = list(commit_errors)
        self.statements: list[str] = []
        # The statement objects themselves, so tests can read their bound parameters.
        self.raw_statements: list[object] = []
        self.added: list[object] = []
        self.added_by: list[tuple[FakeSession, object]] = []
        self.sessions: list[FakeSession] = []
        self.commits = 0
        self.rollbacks = 0

    def session(self) -> "FakeSession":
        session = FakeSession(self)
        self.sessions.append(session)
        return session

    @property
    def factory(self):
        return self.session

    def pop(self, queue: list, statement: str):
        if not queue:
            raise AssertionError(f"Unscripted statement: {statement}")
        item = queue.pop(0)
        return item(self) if callable(item) else item

    def of_type(self, kind):
        return [row for row in self.added if isinstance(row, kind)]

    @property
    def events(self) -> list[PaymentEvent]:
        return self.of_type(PaymentEvent)

    def events_of(self, event_type: str) -> list[PaymentEvent]:
        return [event for event in self.events if event.event_type == event_type]

    @property
    def payments(self) -> list[Payment]:
        return self.of_type(Payment)

    def open_transactions(self) -> list["FakeSession"]:
        return [session for session in self.sessions if session.in_transaction]


class FakeSession:
    def __init__(self, db: FakeDB):
        self.db = db
        self.in_transaction = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        self.in_transaction = False

    def _record(self, statement) -> str:
        self.in_transaction = True
        sql = compiled(statement)
        self.db.statements.append(sql)
        self.db.raw_statements.append(statement)
        return sql

    async def scalar(self, statement, params=None):
        del params
        return self.db.pop(self.db.scalar_queue, self._record(statement))

    async def scalars(self, statement):
        return FakeScalars(self.db.pop(self.db.list_queue, self._record(statement)))

    async def execute(self, statement, params=None):
        del params
        sql = self._record(statement)
        if "pg_advisory_xact_lock(hashtext(%(hashtext_1)s" in sql:
            # The per buyer payment lock (a bound key) returns nothing useful, so it is recorded but
            # never scripted. Other advisory locks, such as the invite lock, are still scripted.
            return FakeResult()
        return self.db.pop(self.db.result_queue, sql)

    def add(self, row):
        self.in_transaction = True
        self.db.added.append(row)
        self.db.added_by.append((self, row))

    async def flush(self):
        return None

    async def commit(self):
        self.in_transaction = False
        if self.db.commit_errors:
            error = self.db.commit_errors.pop(0)
            if error is not None:
                raise error
        self.db.commits += 1

    async def rollback(self):
        self.in_transaction = False
        self.db.rollbacks += 1

    async def close(self):
        self.in_transaction = False


ACCEPTED_PUSH = StkPushResult(
    accepted=True,
    merchant_request_id="29115-34620561-1",
    checkout_request_id="ws_CO_191220191020363925",
    description="Success. Request accepted for processing",
    request={"PhoneNumber": "254*******678"},
    response={"http_status": 200, "ResponseCode": "0"},
)
REJECTED_PUSH = StkPushResult(
    accepted=False,
    merchant_request_id=None,
    checkout_request_id=None,
    description="Bad Request - Invalid PhoneNumber",
    request={},
    response={"http_status": 400, "errorCode": "400.002.02"},
)


class FakeDaraja:
    """Records calls and the database transactions that were open while Daraja was being called."""

    def __init__(self, db: FakeDB | None = None, push=ACCEPTED_PUSH, push_error=None, query=None, query_error=None):
        self.db = db
        self.push = push
        self.push_error = push_error
        self.query = query
        self.query_error = query_error
        self.pushes: list[dict[str, object]] = []
        self.queries: list[str] = []
        self.open_during_push: list[FakeSession] | None = None

    async def stk_push(self, phone, amount, account_reference, callback_url):
        self.pushes.append(
            {"phone": phone, "amount": amount, "account_reference": account_reference, "callback_url": callback_url}
        )
        if self.db is not None:
            self.open_during_push = self.db.open_transactions()
        if self.push_error is not None:
            raise self.push_error
        return self.push

    async def stk_query(self, checkout_request_id):
        self.queries.append(checkout_request_id)
        if self.query_error is not None:
            raise self.query_error
        return self.query


def query_result(state: str, code: int | None) -> StkQueryResult:
    return StkQueryResult(state=state, result_code=code, description=f"{state} result", response={})


def make_buyer(**overrides):
    values = {
        "id": uuid4(),
        "role": UserRole.BUYER,
        "name": "Test Buyer",
        "phone_number": BUYER_PHONE,
        "is_disabled": False,
        "must_change_password": False,
        "is_super_admin": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def make_order(buyer_id, **overrides) -> Order:
    values = {
        "id": uuid4(),
        "reference": "K7QX2MPA",
        "listing_id": uuid4(),
        "buyer_id": buyer_id,
        "buyer_role": UserRole.BUYER,
        "quantity": Decimal("2.50"),
        "unit_price": Decimal("45.50"),
        "total_amount": 114,
        "status": OrderStatus.PENDING_PAYMENT,
        "expires_at": NOW + timedelta(minutes=5),
        "created_at": NOW,
    }
    values.update(overrides)
    return Order(**values)


def make_payment(order: Order, **overrides) -> Payment:
    values = {
        "id": uuid4(),
        "order_id": order.id,
        "idempotency_key": f"key-{uuid4()}",
        "phone_number": BUYER_PHONE,
        "stk_phone_number": BUYER_PHONE,
        "amount": order.total_amount,
        "status": PaymentStatus.INITIATED,
        "checkout_request_id": "ws_CO_191220191020363925",
        "merchant_request_id": "29115-34620561-1",
        "environment": "sandbox",
        "created_at": NOW - timedelta(seconds=30),
    }
    values.update(overrides)
    return Payment(**values)


def view_row(order: Order, **overrides):
    """A row shaped like the joined order view query, with the database clock at NOW."""
    row = {
        "id": order.id,
        "reference": order.reference,
        "buyer_id": order.buyer_id,
        "status": str(order.status),
        "quantity": order.quantity,
        "unit_price": order.unit_price,
        "total_amount": order.total_amount,
        "expires_at": order.expires_at,
        "created_at": NOW,
        "listing_id": order.listing_id,
        "crop": "maize",
        "market": "Nairobi",
        "unit": "kg",
        "image_path": None,
        "listing_status": "active",
        "seller_name": "Farmer Jane",
        "payment_status": None,
        "mpesa_receipt_number": None,
        "result_desc": None,
        "payment_phone_number": None,
        "payment_stk_phone_number": None,
        "payment_completed_at": None,
        "payment_attempts": 0,
        "has_open_payment": False,
        "server_time": NOW,
    }
    row.update(overrides)
    return row


def callback_body(checkout_request_id="ws_CO_191220191020363925", result_code=0, amount=114, receipt="SBX1A2B3C4"):
    callback: dict[str, object] = {
        "MerchantRequestID": "29115-34620561-1",
        "CheckoutRequestID": checkout_request_id,
        "ResultCode": result_code,
        "ResultDesc": "The service request is processed successfully." if result_code == 0 else "Not paid",
    }
    if result_code == 0:
        callback["CallbackMetadata"] = {
            "Item": [
                {"Name": "PhoneNumber", "Value": "254*******149"},
                {"Name": "MpesaReceiptNumber", "Value": receipt},
                {"Name": "TransactionDate", "Value": 20261014090102},
                {"Name": "Amount", "Value": amount},
            ]
        }
    return {"Body": {"stkCallback": callback}}
