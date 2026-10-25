"""Manual end-to-end check of the farmer, buyer and admin journeys against a RUNNING backend, over HTTP.

This is not part of ``pytest``. Start the API first, preferably with rate limits disabled because the run
registers and signs in several times (registration is limited to 10 per hour, sign in to 5 per minute
and code verification to 5 or 10 per minute per IP otherwise):

    RATE_LIMITS_DISABLED=1 EMAIL_PROVIDER=console python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
    python scripts/e2e_check.py

Variables (read from the process environment):

    E2E_BASE_URL            default http://localhost:8000
    E2E_ALLOW_REMOTE        set to 1 to allow a host other than localhost (for example a deployed API)
    E2E_ADMIN_EMAIL         the super admin's email
    E2E_ADMIN_PASSWORD      the super admin's password (an unchanged seeded password is fine for sign in,
                            but admin routes then answer 403 password_change_required and are skipped)
    E2E_MAILBOX_DIR         the console provider's mailbox (default ./.dev_mailbox)
    E2E_ORIGIN              a browser origin the server allows (default: first entry of CORS_ORIGINS)
    E2E_PAYMENTS            skip (default) | simulate | prompt
    E2E_INVITES             skip (default) | console
    E2E_CONFIRM_REAL_PROMPT yes, required for E2E_PAYMENTS=prompt (the prompt may reach a real phone)
    DATABASE_URL            only for E2E_PAYMENTS=simulate, to read the payment's checkout request id
    MPESA_CALLBACK_TOKEN    only for E2E_PAYMENTS=simulate, to post the simulated callback

Sign in. Accounts are email only. Registration and every password sign in need an emailed code, which
this script reads from the newest file in ``E2E_MAILBOX_DIR`` that contains the recipient's address.
The auth steps (and so everything that needs a session) therefore run only when the server's
``EMAIL_PROVIDER`` is ``console`` and the server writes to the same directory; otherwise they are
skipped with a reason. A resend has a cooldown (``LOGIN_RESEND_COOLDOWN_SECONDS``, 60 by default), and
the farmer's sign in waits for it once to check that an old code stops working after a resend.

Payment modes. ``simulate`` asks the server for a real sandbox STK push (start the server with
``MPESA_STK_TARGET=test_msisdn`` so it goes to the sandbox test number), then posts a simulated success
callback itself. Daraja may later send its own callback for the same request; the server treats it as a
duplicate. ``prompt`` waits up to 150 seconds for someone to approve the prompt on the phone. Both send
the number entered for the payment as ``{"phone_number": ...}`` (required); ``simulate`` also checks
that a missing number is 422 ``phone_required`` and replays the key with the same number (same result)
and with a different number (409 ``idempotency_key_reused``).

Safety: the script refuses to run when ``APP_ENV`` is ``production`` and, unless ``E2E_ALLOW_REMOTE=1``,
when the base URL is not localhost. It never prints tokens, passwords, codes, full email addresses or
full phone numbers. Test users are named ``ZZ_E2E_<random>``; users cannot be deleted (a database
rule), so the cleanup disables them through the admin API and reports how many disabled ``ZZ_E2E_``
users remain.

Every step is recorded as PASS, FAIL or SKIP with a reason; the exit code is 1 when any step failed.
"""

import asyncio
import os
import re
import secrets
import struct
import sys
import time
import zlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "ZZ_E2E_"
CROPS = ("maize", "beans")
HORIZONS = ("one_month", "two_month")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
CODE_IN_EMAIL = re.compile(r"\b[A-Z2-9]{4}-[A-Z2-9]{4}\b")
MAILBOX_WAIT_SECONDS = 10
REGISTRATION_KEYS = {"message", "registration_token", "email_masked", "code_expires_at", "resend_available_in"}
CALLBACK_PATH = re.compile(r"/payments/callback/[^/?#\s]+")
DISALLOWED_ORIGIN = "https://zz-e2e-disallowed.example"
POLL_SECONDS = 3
PROMPT_TIMEOUT_SECONDS = 150
SIMULATE_TIMEOUT_SECONDS = 30

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


class StepFailed(Exception):
    """A failed expectation; the message is written by this script and contains no secret."""


class StepSkipped(Exception):
    """The data or setting a step needs is not available."""


@dataclass
class Result:
    section: str
    name: str
    status: str
    reason: str


@dataclass
class Actor:
    label: str
    name: str
    email: str
    password: str
    pay_phone: str = ""
    id: str | None = None
    token: str | None = None

    @property
    def auth(self) -> dict[str, str]:
        if not self.token:
            raise StepSkipped(f"no session for the {self.label}")
        return {"Authorization": f"Bearer {self.token}"}

    @property
    def masked_pay_phone(self) -> str:
        return f"254*******{self.pay_phone[-3:]}"

    @property
    def masked_email(self) -> str:
        local, _, domain = self.email.partition("@")
        return f"{local[:1]}***@{domain[:1]}***"


class Mailbox:
    """Reads codes that the console email provider writes, one file per message."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def snapshot(self) -> set[Path]:
        return set(self.directory.glob("*")) if self.directory.is_dir() else set()

    async def code_for(self, recipient: str, before: set[Path]) -> str:
        """The code in the newest new file that contains ``recipient``; waits for the background send."""
        deadline = time.monotonic() + MAILBOX_WAIT_SECONDS
        while True:
            new_files = sorted(self.snapshot() - before, key=lambda path: path.stat().st_mtime, reverse=True)
            for path in new_files:
                content = path.read_text(encoding="utf-8", errors="replace")
                if recipient.lower() in content.lower():
                    match = CODE_IN_EMAIL.search(content)
                    if match is None:
                        raise StepFailed("the newest email for this address contains no code")
                    return match.group(0)
            if time.monotonic() > deadline:
                raise StepFailed("no new email for this address in the mailbox (is the server on this machine?)")
            await asyncio.sleep(0.5)

    async def nothing_new_for(self, recipient: str, before: set[Path], wait: float = 1.5) -> bool:
        await asyncio.sleep(wait)
        return not any(
            recipient.lower() in path.read_text(encoding="utf-8", errors="replace").lower()
            for path in self.snapshot() - before
        )


def other_code(code: str) -> str:
    """A well formed code that differs from ``code``."""
    return "AAAA-AAAA" if code.replace("-", "") != "AAAAAAAA" else "BBBB-BBBB"


def random_phone() -> str:
    return "2541" + "".join(secrets.choice("0123456789") for _ in range(8))


def different_phone(phone: str) -> str:
    """Another valid number that differs from ``phone`` in its last digit."""
    return phone[:-1] + ("1" if phone[-1] != "1" else "2")


def random_password() -> str:
    # Upper, lower, digit and symbol, 12+ characters: valid for every role's policy.
    return f"E2e-{secrets.token_urlsafe(15)}-7q!"


def tiny_png() -> bytes:
    """A valid 8x8 green PNG built without any imaging library."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"\x2e\x8b\x57" * 8 for _ in range(8))
    header = struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def safe_path(path: str) -> str:
    return CALLBACK_PATH.sub("/payments/callback/***", path)


def error_code(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error", {}).get("code", "-"))
    except (ValueError, AttributeError):
        return "-"


def expect(response: httpx.Response, status: int, code: str | None = None) -> Any:
    """Check a status (and error code); return the JSON body when there is one."""
    if response.status_code != status:
        raise StepFailed(f"expected {status}, got {response.status_code} ({error_code(response)})")
    if code is not None and error_code(response) != code:
        raise StepFailed(f"expected error code {code}, got {error_code(response)}")
    if response.content and response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    return None


def env_or_dotenv(name: str) -> str:
    """Read a setting from the environment, then from the repository's .env (never printed)."""
    if os.environ.get(name):
        return os.environ[name]
    env_file = ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip() == name:
                return value.strip().strip("\"'")
    return ""


def refuse_unsafe_target(base_url: str) -> str | None:
    if env_or_dotenv("APP_ENV").strip().lower() == "production":
        return "APP_ENV is production; this script never runs against production configuration."
    host = urlsplit(base_url).hostname or ""
    if host not in LOCAL_HOSTS and os.environ.get("E2E_ALLOW_REMOTE") != "1":
        return f"{base_url} is not localhost; set E2E_ALLOW_REMOTE=1 to run against another host."
    return None


class E2ERun:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.results: list[Result] = []
        self.header_violations: list[str] = []
        self.client = httpx.AsyncClient(
            base_url=self.base_url, timeout=30.0, event_hooks={"response": [self._check_headers]}
        )
        suffix = secrets.token_hex(3)
        self.farmer = Actor(
            "farmer", f"{PREFIX}farmer_{suffix}", f"zz_e2e_farmer_{suffix}@example.com", random_password()
        )
        self.buyer = Actor(
            "buyer",
            f"{PREFIX}buyer_{suffix}",
            f"zz_e2e_buyer_{suffix}@example.com",
            random_password(),
            pay_phone=random_phone(),
        )
        self.admin = Actor(
            "super admin",
            "super admin",
            os.environ.get("E2E_ADMIN_EMAIL", "").strip().lower(),
            os.environ.get("E2E_ADMIN_PASSWORD", ""),
        )
        self.invited = Actor(
            "invited admin", f"{PREFIX}admin_{suffix}", f"zz_e2e_admin_{suffix}@example.com", random_password()
        )
        self.mailbox = Mailbox(Path(os.environ.get("E2E_MAILBOX_DIR", "./.dev_mailbox")))
        provider = env_or_dotenv("EMAIL_PROVIDER").strip().lower() or "console"
        self.auth_skip_reason = (
            None
            if provider == "console"
            else "the server's EMAIL_PROVIDER is not console, so codes cannot be read from the mailbox"
        )
        self.markets: list[str] = []
        self.listing: dict[str, Any] | None = None
        self.listing_ids: list[str] = []
        self.order: dict[str, Any] | None = None
        self.admin_ready = False

    async def _check_headers(self, response: httpx.Response) -> None:
        if not response.headers.get("x-request-id") or response.headers.get("cache-control") != "no-store":
            request = response.request
            self.header_violations.append(f"{request.method} {safe_path(request.url.path)} {response.status_code}")

    async def step(self, section: str, name: str, action: Callable[[], Awaitable[str | None]]) -> bool:
        try:
            note = await action()
        except StepSkipped as exc:
            self.results.append(Result(section, name, SKIP, str(exc)))
            return False
        except StepFailed as exc:
            self.results.append(Result(section, name, FAIL, str(exc)))
            return False
        except httpx.HTTPError as exc:
            self.results.append(Result(section, name, FAIL, f"HTTP error {type(exc).__name__}"))
            return False
        except Exception as exc:  # noqa: BLE001 - the type is enough; messages may carry URLs or values
            self.results.append(Result(section, name, FAIL, f"unexpected {type(exc).__name__}"))
            return False
        self.results.append(Result(section, name, PASS, note or ""))
        return True

    async def api(self, method: str, path: str, actor: Actor | None = None, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        if actor is not None:
            headers.update(actor.auth)
        return await self.client.request(method, f"/api/v1{path}", headers=headers, **kwargs)

    # 1. System --------------------------------------------------------------------------------

    async def system(self) -> None:
        async def health() -> str:
            body = expect(await self.client.get("/api/v1/health"), 200)
            if body != {"status": "ok"}:
                raise StepFailed("unexpected health body")
            return "ok"

        async def database_health() -> str:
            expect(await self.client.get("/api/v1/health/db"), 200)
            return "database reachable"

        async def cors() -> str:
            origin = os.environ.get("E2E_ORIGIN") or next(
                (item.strip() for item in env_or_dotenv("CORS_ORIGINS").split(",") if item.strip()), ""
            )
            if not origin:
                raise StepSkipped("set E2E_ORIGIN or CORS_ORIGINS to an allowed origin")
            preflight = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"}
            allowed = await self.client.options("/api/v1/auth/login", headers={"Origin": origin, **preflight})
            if allowed.headers.get("access-control-allow-origin") != origin:
                raise StepFailed("the allowed origin was not granted")
            refused = await self.client.options(
                "/api/v1/auth/login", headers={"Origin": DISALLOWED_ORIGIN, **preflight}
            )
            if "access-control-allow-origin" in refused.headers:
                raise StepFailed("a disallowed origin was granted")
            return "allowed origin granted, other origin refused"

        async def unknown_route() -> str:
            response = await self.client.get(f"/api/v1/zz-e2e-unknown-{uuid4().hex}")
            body = expect(response, 404)
            error = (body or {}).get("error", {})
            if not {"code", "message", "request_id"} <= set(error):
                raise StepFailed("the 404 body is not the uniform error body")
            if error["request_id"] != response.headers.get("x-request-id"):
                raise StepFailed("request_id in the body differs from the X-Request-ID header")
            return "uniform 404 body"

        await self.step("system", "health", health)
        await self.step("system", "database health", database_health)
        await self.step("system", "CORS preflight", cors)
        await self.step("system", "unknown route body", unknown_route)

    # 2. Farmer --------------------------------------------------------------------------------

    def require_auth_steps(self) -> None:
        if self.auth_skip_reason:
            raise StepSkipped(self.auth_skip_reason)

    async def register(self, actor: Actor, route: str) -> None:
        """Register with an emailed code: same 202 shape twice, a wrong code fails, the right one signs in."""
        self.require_auth_steps()
        body = {"name": actor.name, "email": actor.email, "password": actor.password}
        before = self.mailbox.snapshot()
        first = expect(await self.api("POST", f"/auth/register/{route}", json=body), 202)
        if set(first) != REGISTRATION_KEYS:
            raise StepFailed("the registration response has unexpected fields")
        code = await self.mailbox.code_for(actor.email, before)
        # The same address again inside the resend cooldown: the identical shape, a new token, the same code.
        before_repeat = self.mailbox.snapshot()
        second = expect(await self.api("POST", f"/auth/register/{route}", json=body), 202)
        if set(second) != set(first) or second["message"] != first["message"]:
            raise StepFailed("a repeated registration answered differently")
        if not await self.mailbox.nothing_new_for(actor.email, before_repeat):
            raise StepFailed("a repeated registration inside the cooldown sent another code")
        token = second["registration_token"]
        wrong = {"registration_token": token, "code": other_code(code)}
        expect(await self.api("POST", "/auth/register/verify", json=wrong), 400, "invalid_or_expired_code")
        old = {"registration_token": first["registration_token"], "code": code}
        expect(await self.api("POST", "/auth/register/verify", json=old), 400, "invalid_or_expired_code")
        verified = expect(
            await self.api("POST", "/auth/register/verify", json={"registration_token": token, "code": code}), 201
        )
        if verified.get("status") != "signed_in" or verified.get("role") != route:
            raise StepFailed(f"verification did not sign in a {route}")
        if verified.get("is_super_admin") is not False:
            raise StepFailed("the verification response does not say is_super_admin false")
        actor.token = verified["access_token"]
        me = expect(await self.api("GET", "/auth/me", actor), 200)
        actor.id = me["id"]
        if me["role"] != route or "phone_number" in me:
            raise StepFailed("/auth/me does not match the registered account")

    async def sign_in(self, actor: Actor, *, check_wrong_password: bool = False, check_resend: bool = False) -> str:
        """Password, then the emailed code. Returns a short note."""
        self.require_auth_steps()
        actor.token = None  # sign out: tokens are stateless, so the client simply drops it
        if not actor.email or not actor.password:
            raise StepSkipped(f"no email or password for the {actor.label}")
        if check_wrong_password:
            before = self.mailbox.snapshot()
            wrong = {"email": actor.email, "password": actor.password + "x"}
            expect(await self.api("POST", "/auth/login", json=wrong), 401, "invalid_credentials")
            if not await self.mailbox.nothing_new_for(actor.email, before, wait=1.0):
                raise StepFailed("a wrong password sent a sign in code")
        before = self.mailbox.snapshot()
        credentials = {"email": actor.email, "password": actor.password}
        body = expect(await self.api("POST", "/auth/login", json=credentials), 200)
        if body.get("status") == "signed_in":
            actor.token = body["access_token"]
            return "signed in with the password alone (role not in LOGIN_OTP_ROLES)"
        if body.get("status") != "code_required" or "access_token" in body:
            raise StepFailed("sign in did not ask for a code")
        if not body.get("code_sent"):
            raise StepFailed("the server could not send the sign in code")
        challenge = body["challenge_token"]
        code = await self.mailbox.code_for(actor.email, before)
        note = "password and code"
        if check_resend:
            await asyncio.sleep(max(0, int(body.get("resend_available_in", 0))) + 1)
            before_resend = self.mailbox.snapshot()
            resent = expect(await self.api("POST", "/auth/login/resend", json={"challenge_token": challenge}), 200)
            if not resent.get("code_sent"):
                raise StepFailed("the resend did not send a code")
            new_code = await self.mailbox.code_for(actor.email, before_resend)
            if new_code != code:
                old = {"challenge_token": challenge, "code": code}
                expect(await self.api("POST", "/auth/login/verify", json=old), 400, "invalid_or_expired_code")
            code = new_code
            note = "password and code; the previous code failed after a resend"
        verified = expect(
            await self.api("POST", "/auth/login/verify", json={"challenge_token": challenge, "code": code}), 200
        )
        if verified.get("status") != "signed_in":
            raise StepFailed("the code did not sign in")
        actor.token = verified["access_token"]
        return note

    async def farmer_journey(self) -> None:
        farmer = self.farmer

        async def forbidden_fields() -> str:
            base = {"name": farmer.name, "email": farmer.email, "password": farmer.password}
            for extra in ({"role": "admin"}, {"phone_number": "0712345678"}):
                expect(await self.api("POST", "/auth/register/farmer", json={**base, **extra}), 422)
            return "422 for a body with role or phone_number"

        async def register() -> str:
            await self.register(farmer, "farmer")
            return f"farmer {farmer.masked_email}"

        async def sign_in_again() -> str:
            return await self.sign_in(farmer, check_wrong_password=True, check_resend=True)

        async def markets() -> str:
            body = expect(await self.api("GET", "/markets", farmer), 200)
            self.markets = [item["name"] for item in body]
            if not self.markets:
                raise StepSkipped("no market has price data yet")
            return ", ".join(self.markets)

        async def current_prices() -> str:
            counts = []
            for crop in CROPS:
                body = expect(await self.api("GET", "/prices/current", farmer, params={"crop": crop}), 200)
                counts.append(f"{crop} {len(body['items'])}")
            return ", ".join(counts)

        async def trends() -> str:
            if not self.markets:
                raise StepSkipped("no markets")
            for market in self.markets:
                for crop in CROPS:
                    params = {"market": market, "crop": crop, "months": 12}
                    expect(await self.api("GET", "/prices/trends", farmer, params=params), 200)
            return f"{len(self.markets) * len(CROPS)} series"

        async def forecasts() -> str:
            if not self.markets:
                raise StepSkipped("no markets")
            found = missing = 0
            for market in self.markets:
                for crop in CROPS:
                    for horizon in HORIZONS:
                        params = {"market": market, "crop": crop, "horizon": horizon}
                        response = await self.api("GET", "/forecasts", farmer, params=params)
                        if response.status_code == 404 and error_code(response) == "forecast_unavailable":
                            missing += 1
                            continue
                        body = expect(response, 200)
                        if "shap_values" not in body or "expected_change_pct" not in body:
                            raise StepFailed("forecast without shap_values or expected_change_pct")
                        found += 1
            if found == 0:
                raise StepSkipped(f"no forecast available yet ({missing} combinations checked)")
            return f"{found} forecasts, {missing} not available"

        async def create_listings() -> str:
            if not self.markets:
                raise StepSkipped("no market with prices")
            market = self.markets[0]
            price = Decimal("50")
            current = expect(await self.api("GET", "/prices/current", farmer, params={"crop": "maize"}), 200)
            for item in current["items"]:
                if item["market"] == market and item.get("avg_price_per_kg"):
                    price = max(Decimal(str(item["avg_price_per_kg"])), Decimal("10")).quantize(Decimal("0.01"))
            form = {"crop": "maize", "market": market, "quantity_available": "25", "unit": "kg"}
            plain = expect(
                await self.api("POST", "/listings", farmer, data={**form, "price_per_unit": str(price)}), 201
            )
            self.listing = plain
            self.listing_ids.append(plain["id"])
            image = expect(
                await self.api(
                    "POST",
                    "/listings",
                    farmer,
                    data={**form, "price_per_unit": str(price + 1)},
                    files={"image": ("e2e.png", tiny_png(), "image/png")},
                ),
                201,
            )
            self.listing_ids.append(image["id"])
            if not image.get("image_url"):
                raise StepFailed("the listing with an image has no image_url")
            async with httpx.AsyncClient(timeout=30.0) as plain_client:  # no API credentials to another host
                fetched = await plain_client.get(image["image_url"])
            if fetched.status_code != 200 or not fetched.headers.get("content-type", "").startswith("image/"):
                raise StepFailed(f"image_url answered {fetched.status_code}")
            mine = expect(await self.api("GET", "/listings/mine", farmer, params={"limit": 100}), 200)
            ids = {item["id"] for item in mine["items"]}
            if not {plain["id"], image["id"]} <= ids:
                raise StepFailed("/listings/mine does not contain both listings")
            for quantity, status in (("0", "sold_out"), ("25", "active")):
                edited = expect(
                    await self.api("PATCH", f"/listings/{image['id']}", farmer, data={"quantity_available": quantity}),
                    200,
                )
                if edited["status"] != status:
                    raise StepFailed(f"quantity {quantity} gave status {edited['status']}")
            expect(await self.api("DELETE", f"/listings/{image['id']}", farmer), 204)
            return f"2 listings in {market} at {price}/kg, image served, edit and delete ok"

        async def read_own_listing() -> str:
            if self.listing is None:
                raise StepSkipped("no e2e listing (see the listings step)")
            listing_id = self.listing["id"]
            single = expect(await self.api("GET", f"/listings/mine/{listing_id}", farmer), 200)
            mine = expect(await self.api("GET", "/listings/mine", farmer, params={"limit": 100}), 200)
            item = next((entry for entry in mine["items"] if entry["id"] == listing_id), None)
            if item is None or single != item:
                raise StepFailed("GET /listings/mine/{id} differs from the /listings/mine item")
            removed = [entry for entry in mine["items"] if entry["status"] == "removed"]
            if removed:
                body = expect(await self.api("GET", f"/listings/mine/{removed[0]['id']}", farmer), 200)
                if body["status"] != "removed":
                    raise StepFailed("a removed listing is not returned as removed")
            expect(await self.api("GET", f"/listings/mine/{uuid4()}", farmer), 404, "listing_not_found")
            return "own listing read (active and removed), unknown id 404"

        async def farmer_forbidden() -> str:
            expect(await self.api("GET", "/listings", farmer), 403, "forbidden")
            expect(await self.api("POST", "/orders", farmer, json={}), 403, "forbidden")
            expect(await self.api("GET", "/orders/mine", farmer), 403, "forbidden")
            return "buyer routes answer 403"

        await self.step("farmer", "register with role or phone_number is 422", forbidden_fields)
        if await self.step("farmer", "register with an emailed code, me", register):
            await self.step("farmer", "sign out, sign in with password and code", sign_in_again)
            await self.step("farmer", "markets", markets)
            await self.step("farmer", "current prices", current_prices)
            await self.step("farmer", "trends", trends)
            await self.step("farmer", "forecasts", forecasts)
            await self.step("farmer", "listings", create_listings)
            await self.step("farmer", "read own listing", read_own_listing)
            await self.step("farmer", "buyer routes forbidden", farmer_forbidden)

    # 3. Buyer ---------------------------------------------------------------------------------

    async def buyer_journey(self) -> None:
        buyer = self.buyer

        async def register() -> str:
            await self.register(buyer, "buyer")
            note = await self.sign_in(buyer)
            return f"buyer {buyer.masked_email}; signed in again with {note}"

        async def buyer_forbidden() -> str:
            expect(await self.api("GET", "/prices/current", buyer, params={"crop": "maize"}), 403, "forbidden")
            params = {"market": "Nairobi", "crop": "maize", "horizon": "one_month"}
            expect(await self.api("GET", "/forecasts", buyer, params=params), 403, "forbidden")
            return "prices and forecasts answer 403"

        def need_listing() -> dict[str, Any]:
            if self.listing is None:
                raise StepSkipped("no e2e listing (see the farmer steps)")
            return self.listing

        async def browse() -> str:
            listing = need_listing()
            params = {"crop": "maize", "market": listing["market"], "sort": "price_asc", "min_price": "0", "limit": 100}
            body = expect(await self.api("GET", "/listings", buyer, params=params), 200)
            prices = [Decimal(str(item["price_per_unit"])) for item in body["items"]]
            if prices != sorted(prices):
                raise StepFailed("price_asc is not ascending")
            if listing["id"] not in {item["id"] for item in body["items"]}:
                raise StepFailed("the e2e listing is not in the filtered browse")
            detail = expect(await self.api("GET", f"/listings/{listing['id']}", buyer), 200)
            if not detail["available"]:
                raise StepFailed("the e2e listing is not available")
            return f"{body['total']} listings match"

        async def browse_markets() -> str:
            listing = need_listing()
            markets = expect(await self.api("GET", "/listings/markets", buyer), 200)
            names = [item["name"] for item in markets]
            if names != sorted(names):
                raise StepFailed("/listings/markets is not ordered by name")
            if any(set(item) != {"name", "listing_count"} for item in markets):
                raise StepFailed("/listings/markets returns fields other than name and listing_count")
            entry = next((item for item in markets if item["name"] == listing["market"]), None)
            if entry is None or entry["listing_count"] < 1:
                raise StepFailed("the e2e listing's market is not in /listings/markets")
            filtered = expect(
                await self.api("GET", "/listings", buyer, params={"market": listing["market"], "limit": 1}), 200
            )
            if filtered["total"] != entry["listing_count"]:
                raise StepFailed("listing_count differs from the browse total for that market")
            return f"{len(markets)} markets; {listing['market']} has {entry['listing_count']} listings"

        async def orders() -> str:
            listing = need_listing()
            quantity = Decimal("2")
            body = {"listing_id": listing["id"], "quantity": str(quantity)}
            first = expect(await self.api("POST", "/orders", buyer, json=body), 201)
            expected_total = (quantity * Decimal(str(listing["price_per_unit"]))).to_integral_value(ROUND_CEILING)
            if Decimal(str(first["total_amount"])) != expected_total:
                raise StepFailed("the order total is not ceil(quantity x price)")
            repeat = await self.api("POST", "/orders", buyer, json=body)
            expect(repeat, 409, "pending_order_exists")
            if repeat.json()["error"].get("existing_order_id") != first["id"]:
                raise StepFailed("409 does not name the existing order")
            before = expect(await self.api("GET", f"/listings/{listing['id']}", buyer), 200)
            cancelled = expect(await self.api("POST", f"/orders/{first['id']}/cancel", buyer), 200)
            if cancelled["status"] != "cancelled":
                raise StepFailed("cancel did not cancel")
            after = expect(await self.api("GET", f"/listings/{listing['id']}", buyer), 200)
            returned = Decimal(str(after["quantity_available"])) - Decimal(str(before["quantity_available"]))
            if returned != quantity:
                raise StepFailed("cancelling did not return the stock")
            self.order = expect(await self.api("POST", "/orders", buyer, json=body), 201)
            mine = expect(await self.api("GET", "/orders/mine", buyer), 200)
            if self.order["id"] not in {item["id"] for item in mine["items"]}:
                raise StepFailed("the new order is not in /orders/mine")
            expect(await self.api("GET", f"/orders/{self.order['id']}", buyer), 200)
            return f"total {expected_total} KES, 409 on repeat, stock returned on cancel"

        if await self.step("buyer", "register, sign out, sign in with password and code", register):
            await self.step("buyer", "prices and forecasts forbidden", buyer_forbidden)
            await self.step("buyer", "browse and detail", browse)
            await self.step("buyer", "markets filter", browse_markets)
            await self.step("buyer", "orders", orders)

    # 4. Payments ------------------------------------------------------------------------------

    async def poll_until_paid(self, timeout: int) -> dict[str, Any]:
        assert self.order is not None
        deadline = time.monotonic() + timeout
        while True:
            current = expect(await self.api("GET", f"/orders/{self.order['id']}", self.buyer), 200)
            if current["status"] == "paid":
                return current
            if current["status"] not in ("pending_payment",) or time.monotonic() > deadline:
                raise StepFailed(f"order ended as {current['status']} instead of paid")
            await asyncio.sleep(POLL_SECONDS)

    async def payments(self) -> None:
        mode = os.environ.get("E2E_PAYMENTS", "skip").strip().lower()
        if mode == "skip":
            self.results.append(Result("payments", "payment", SKIP, "E2E_PAYMENTS=skip"))
            return
        if self.order is None:
            self.results.append(Result("payments", "payment", SKIP, "no pending e2e order"))
            return
        if mode == "simulate":
            await self.step("payments", "simulated payment", self.simulated_payment)
        elif mode == "prompt":
            await self.step("payments", "real sandbox prompt", self.prompt_payment)
        else:
            self.results.append(Result("payments", "payment", FAIL, "E2E_PAYMENTS must be skip, simulate or prompt"))

    async def pay(
        self, key: str, phone: str | None = None, status: int = 202, code: str | None = None
    ) -> dict[str, Any]:
        """POST /pay with the M-Pesa number in the body, or with no body at all when ``phone`` is None (422)."""
        assert self.order is not None
        kwargs: dict[str, Any] = {"headers": {"Idempotency-Key": key}}
        if phone is not None:
            kwargs["json"] = {"phone_number": phone}
        return expect(await self.api("POST", f"/orders/{self.order['id']}/pay", self.buyer, **kwargs), status, code)

    async def simulated_payment(self) -> str:
        assert self.order is not None
        database_url = os.environ.get("DATABASE_URL", "")
        token = os.environ.get("MPESA_CALLBACK_TOKEN", "")
        if not database_url or not token:
            raise StepSkipped("simulate needs DATABASE_URL and MPESA_CALLBACK_TOKEN")
        # The number is required: no body is 422 phone_required before anything is stored.
        await self.pay(str(uuid4()), None, 422, "phone_required")
        key = str(uuid4())
        first = await self.pay(key, self.buyer.pay_phone)
        if first["payment"]["phone_masked"] != self.buyer.masked_pay_phone:
            raise StepFailed("the payment does not show the entered number (masked)")
        replay = await self.pay(key, self.buyer.pay_phone)
        if replay["payment"]["attempts"] != first["payment"]["attempts"] or replay["status"] != first["status"]:
            raise StepFailed("replaying the Idempotency-Key with the same number created another payment")
        await self.pay(key, different_phone(self.buyer.pay_phone), 409, "idempotency_key_reused")
        checkout_id, amount = await read_latest_payment(database_url, self.order["id"])
        if not checkout_id:
            raise StepFailed("the payment has no checkout request id")
        callback = {
            "Body": {
                "stkCallback": {
                    "MerchantRequestID": "e2e-simulated",
                    "CheckoutRequestID": checkout_id,
                    "ResultCode": 0,
                    "ResultDesc": "The service request is processed successfully.",
                    "CallbackMetadata": {
                        "Item": [
                            {"Name": "Amount", "Value": amount},
                            {"Name": "MpesaReceiptNumber", "Value": "E2E" + secrets.token_hex(4).upper()},
                            {"Name": "TransactionDate", "Value": int(time.strftime("%Y%m%d%H%M%S"))},
                            {"Name": "PhoneNumber", "Value": "254*******000"},
                        ]
                    },
                }
            }
        }
        expect(await self.client.post(f"/api/v1/payments/callback/{token}", json=callback), 200)
        paid = await self.poll_until_paid(SIMULATE_TIMEOUT_SECONDS)
        farmer_view = await self.api("GET", "/listings/mine/orders", self.farmer, params={"limit": 100})
        body = expect(farmer_view, 200)
        item = next((row for row in body["items"] if row["order_id"] == self.order["id"]), None)
        if item is None or item["status"] != "paid":
            raise StepFailed("the farmer does not see the paid order")
        if (
            self.buyer.pay_phone in farmer_view.text
            or self.buyer.email in farmer_view.text
            or "phone" in " ".join(item)
            or "email" in " ".join(item)
        ):
            raise StepFailed("the farmer's order view exposes buyer contact details")
        expect(await self.client.post(f"/api/v1/payments/callback/{token}", json=callback), 200)
        again = expect(await self.api("GET", f"/orders/{self.order['id']}", self.buyer), 200)
        if again["status"] != "paid" or again["payment"]["attempts"] != paid["payment"]["attempts"]:
            raise StepFailed("a repeated callback changed the order")
        return (
            "paid via simulated callback; no number was 422, same-number replay and duplicate callback "
            "changed nothing, a different number was refused"
        )

    async def prompt_payment(self) -> str:
        if os.environ.get("E2E_CONFIRM_REAL_PROMPT") != "yes":
            raise StepSkipped("set E2E_CONFIRM_REAL_PROMPT=yes; the prompt may reach a real phone")
        started = await self.pay(str(uuid4()), self.buyer.pay_phone)
        print(f"Approve the prompt on the phone ({started['payment']['stk_target_masked']}).", flush=True)
        await self.poll_until_paid(PROMPT_TIMEOUT_SECONDS)
        return "paid after the prompt was approved"

    # 5. Admin ---------------------------------------------------------------------------------

    async def admin_journey(self) -> None:
        admin = self.admin

        async def login() -> str:
            if not admin.email or not admin.password:
                raise StepSkipped("set E2E_ADMIN_EMAIL and E2E_ADMIN_PASSWORD")
            note = await self.sign_in(admin)
            me = expect(await self.api("GET", "/auth/me", admin), 200)
            admin.id = me["id"]
            if me["role"] != "admin":
                raise StepFailed("E2E_ADMIN_EMAIL is not an admin")
            if me.get("is_super_admin") is not True:
                raise StepFailed("/auth/me for the super admin does not say is_super_admin true")
            if me["must_change_password"]:
                raise StepSkipped("the super admin must change the password first; admin routes answer 403")
            self.admin_ready = True
            return f"signed in with {note}"

        async def reads() -> str:
            expect(await self.api("GET", "/admin/stats", admin), 200)
            users = expect(
                await self.api("GET", "/admin/users", admin, params={"role": "farmer", "q": PREFIX, "limit": 100}),
                200,
            )
            if self.farmer.id and self.farmer.id not in {item["id"] for item in users["items"]}:
                raise StepFailed("the e2e farmer is not in the filtered user list")
            if any("phone_number" in item for item in users["items"]):
                raise StepFailed("the admin user list still contains phone numbers")
            for path in ("/admin/listings", "/admin/orders", "/admin/payments", "/admin/forecast-accuracy"):
                expect(await self.api("GET", path, admin), 200)
            return "stats, users, listings, orders, payments, accuracy"

        async def others_forbidden() -> str:
            checked = 0
            for actor in (self.farmer, self.buyer):
                if actor.token:
                    for path in ("/admin/stats", "/admin/users", "/admin/orders"):
                        expect(await self.api("GET", path, actor), 403)
                        checked += 1
            if not checked:
                raise StepSkipped("no farmer or buyer session")
            return f"{checked} admin calls refused"

        async def disable() -> str:
            if not (self.farmer.id and self.buyer.id):
                raise StepSkipped("the e2e farmer or buyer was not created")
            expect(await self.api("PATCH", f"/admin/users/{self.farmer.id}/disable", admin), 200)
            if self.listing is not None and self.buyer.token:
                browse = expect(await self.api("GET", "/listings", self.buyer, params={"limit": 100}), 200)
                if self.listing["id"] in {item["id"] for item in browse["items"]}:
                    raise StepFailed("a disabled farmer's listing is still listed")
                expect(await self.api("GET", f"/listings/{self.listing['id']}", self.buyer), 404, "listing_not_found")
            expect(await self.api("PATCH", f"/admin/users/{self.buyer.id}/disable", admin), 200)
            for actor in (self.farmer, self.buyer):
                expect(await self.api("GET", "/auth/me", actor), 401)
            return "disabled; listing hidden; next requests 401"

        if await self.step("admin", "login", login):
            await self.step("admin", "admin reads", reads)
            await self.step("admin", "farmer and buyer refused", others_forbidden)
            await self.step("admin", "disable e2e users", disable)

    # 6. Invites -------------------------------------------------------------------------------

    async def invites(self) -> None:
        if os.environ.get("E2E_INVITES", "skip").strip().lower() != "console":
            self.results.append(Result("invites", "invite", SKIP, "E2E_INVITES=skip"))
            return
        if env_or_dotenv("EMAIL_PROVIDER").strip().lower() not in ("", "console"):
            self.results.append(Result("invites", "invite", SKIP, "the server's EMAIL_PROVIDER is not console"))
            return
        if not self.admin_ready:
            self.results.append(Result("invites", "invite", SKIP, "no usable super admin session"))
            return
        await self.step("invites", "invite, accept, signed in", self.invite_flow)

    async def invite_flow(self) -> str:
        invited = self.invited
        with_phone = {"name": invited.name, "email": invited.email, "phone_number": "0712345678"}
        expect(await self.api("POST", "/admin/invites", self.admin, json=with_phone), 422)
        before = self.mailbox.snapshot()
        body = {"name": invited.name, "email": invited.email}
        created = expect(await self.api("POST", "/admin/invites", self.admin, json=body), 201)
        if not created["email_sent"]:
            raise StepFailed("the invite email was not sent")
        if "phone_masked" in created:
            raise StepFailed("the invite response still carries a phone number")
        code = await self.mailbox.code_for(invited.email, before)
        accept = {"email": invited.email, "code": code.lower(), "password": invited.password}
        accepted = expect(await self.api("POST", "/auth/accept-invite", json=accept), 201)
        if accepted.get("status") != "signed_in" or not accepted.get("access_token"):
            raise StepFailed("accepting the invite did not sign the new admin in")
        if accepted.get("is_super_admin") is not False or accepted.get("role") != "admin":
            raise StepFailed("the accept-invite response is not an ordinary admin")
        invited.token = accepted["access_token"]
        me = expect(await self.api("GET", "/auth/me", invited), 200)
        invited.id = me["id"]
        expect(await self.api("GET", "/admin/stats", invited), 200)
        first = await self.api("POST", "/auth/accept-invite", json=accept)
        second = await self.api("POST", "/auth/accept-invite", json={**accept, "code": "AAAA-AAAA"})
        expect(first, 400, "invalid_or_expired_code")
        expect(second, 400, "invalid_or_expired_code")
        if _without_request_id(first.json()) != _without_request_id(second.json()):
            raise StepFailed("a repeated accept is distinguishable from a wrong code")
        expect(await self.api("PATCH", f"/admin/users/{invited.id}/disable", self.admin), 200)
        return "accepted and signed in directly, admin route ok, repeat gives the generic 400, disabled"

    # 7. Cleanup -------------------------------------------------------------------------------

    async def cleanup(self) -> None:
        async def run_cleanup() -> str:
            done = []
            if self.buyer.token:
                response = await self.api("GET", "/orders/mine", self.buyer, params={"status": "pending_payment"})
                if response.status_code == 200:
                    for item in response.json()["items"]:
                        await self.api("POST", f"/orders/{item['id']}/cancel", self.buyer)
                        done.append("order cancelled")
            for listing_id in self.listing_ids:
                response = None
                if self.farmer.token:
                    response = await self.api("DELETE", f"/listings/{listing_id}", self.farmer)
                if (response is None or response.status_code != 204) and self.admin_ready:
                    response = await self.api("DELETE", f"/admin/listings/{listing_id}", self.admin)
                if response is not None and response.status_code == 204:
                    done.append("listing removed")
            if self.admin_ready:
                for actor in (self.farmer, self.buyer, self.invited):
                    if actor.id:
                        response = await self.api("PATCH", f"/admin/users/{actor.id}/disable", self.admin)
                        if response.status_code == 200:
                            done.append(f"{actor.label} disabled")
                remaining = await self.api(
                    "GET", "/admin/users", self.admin, params={"q": PREFIX, "is_disabled": "true", "limit": 1}
                )
                if remaining.status_code == 200:
                    print(
                        f"Notice: {remaining.json()['total']} disabled {PREFIX} users remain "
                        "(users cannot be deleted; this is expected).",
                        flush=True,
                    )
            elif any(actor.id for actor in (self.farmer, self.buyer)):
                raise StepFailed("no admin session: the e2e users were left enabled; disable them in the admin UI")
            return ", ".join(sorted(set(done))) or "nothing to clean"

        await self.step("cleanup", "cleanup", run_cleanup)

    def header_check(self) -> None:
        if self.header_violations:
            sample = "; ".join(self.header_violations[:3])
            self.results.append(Result("system", "X-Request-ID and no-store on every response", FAIL, sample))
        else:
            self.results.append(Result("system", "X-Request-ID and no-store on every response", PASS, ""))

    async def run(self) -> int:
        try:
            await self.system()
            await self.farmer_journey()
            await self.buyer_journey()
            await self.payments()
            await self.admin_journey()
            await self.invites()
        finally:
            await self.cleanup()
            self.header_check()
            await self.client.aclose()
        return self.report()

    def report(self) -> int:
        width = max(len(f"{result.section}: {result.name}") for result in self.results)
        print()
        for result in self.results:
            label = f"{result.section}: {result.name}".ljust(width)
            print(f"{result.status:<4}  {label}  {result.reason}")
        failed = sum(result.status == FAIL for result in self.results)
        skipped = sum(result.status == SKIP for result in self.results)
        print(f"\n{len(self.results) - failed - skipped} passed, {failed} failed, {skipped} skipped.")
        return 1 if failed else 0


def _without_request_id(body: dict[str, Any]) -> dict[str, Any]:
    error = {key: value for key, value in body.get("error", {}).items() if key != "request_id"}
    return {**body, "error": error}


async def read_latest_payment(database_url: str, order_id: str) -> tuple[str | None, int]:
    """Read the newest payment's checkout request id and amount (read only, one query)."""
    import asyncpg

    dsn = database_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    connection = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        row = await connection.fetchrow(
            "select checkout_request_id, amount from public.payments"
            " where order_id = $1::uuid order by created_at desc limit 1",
            order_id,
        )
    finally:
        await connection.close()
    if row is None:
        return None, 0
    return row["checkout_request_id"], int(Decimal(row["amount"]))


def main() -> int:
    base_url = os.environ.get("E2E_BASE_URL", "http://localhost:8000")
    refusal = refuse_unsafe_target(base_url)
    if refusal:
        print(f"Refusing to run: {refusal}", file=sys.stderr)
        return 2
    print(
        f"End-to-end check against {base_url} "
        "(tokens, passwords, codes, email addresses and phone numbers are never shown)."
    )
    return asyncio.run(E2ERun(base_url).run())


if __name__ == "__main__":
    raise SystemExit(main())
