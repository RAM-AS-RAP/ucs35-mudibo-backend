"""Create demo farmers, buyers and listings for frontend development and demonstrations.

Usage (from the repository root, with ``.env`` pointing at a development database):

    python -m scripts.seed_demo_data

What it does, against the database in ``DATABASE_URL``:

- creates 2 demo farmers and 2 demo buyers (names start with ``Demo ``) directly in the database with a
  verified email (``email_verified_at = now()``), ``must_change_password = false`` and random passwords
  that are printed ONCE to this terminal; an account that already exists (looked up by email) is left
  unchanged and its password is not shown again;
- creates about 8 listings for maize and beans in the markets that currently have prices in
  ``market_prices``, with varied quantities and prices around the latest monthly price, unless the demo
  farmers already have active listings; no images.

Demo emails come from ``DEMO_EMAIL_BASE``: ``you@gmail.com`` becomes ``you+demo-farmer1@gmail.com``,
which Gmail delivers to your own inbox. Without it they are ``demo.farmer1@example.com`` style
addresses, which receive nothing.

Signing in as a demo account needs the emailed sign in code, so use ``EMAIL_PROVIDER=console`` (the
code is written to ``.dev_mailbox/``) or the plus addresses above.

It refuses to run when ``APP_ENV`` is ``production`` and stops with a message when there are no prices yet
(run the pipeline or ``scripts/seed_prices.py`` first). Running it again changes nothing.
"""

import asyncio
import secrets
from decimal import ROUND_HALF_UP, Decimal

from pydantic import EmailStr, TypeAdapter
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.core.security import hash_password
from app.db import get_sessionmaker
from app.models.enums import ListingStatus, UserRole
from app.models.listing import ProduceListing
from app.models.user import User
from app.services import listing_service

# Fixed demo accounts: (name, address label).
DEMO_FARMERS = (("Demo Farmer Wanjiku", "farmer1"), ("Demo Farmer Otieno", "farmer2"))
DEMO_BUYERS = (("Demo Buyer Amina", "buyer1"), ("Demo Buyer Kamau", "buyer2"))
DEFAULT_DEMO_DOMAIN = "example.com"

# Quantity (kg) and price factor (times the latest price) for each listing slot, in order.
LISTING_PLAN = (
    (Decimal("40"), Decimal("0.95")),
    (Decimal("120"), Decimal("1.00")),
    (Decimal("75"), Decimal("1.05")),
    (Decimal("250"), Decimal("0.90")),
    (Decimal("60"), Decimal("1.10")),
    (Decimal("180"), Decimal("1.02")),
    (Decimal("90"), Decimal("0.98")),
    (Decimal("30"), Decimal("1.08")),
)

LATEST_PRICES_SQL = """
    select distinct on (market, crop) market, crop, avg_price_per_kg
    from public.market_prices
    where crop in ('maize', 'beans')
    order by market, crop, month desc
"""


def demo_email(label: str, base: str = "") -> str:
    """``you@gmail.com`` and ``farmer1`` give ``you+demo-farmer1@gmail.com``; no base gives example.com."""
    base = base.strip()
    if not base:
        return f"demo.{label}@{DEFAULT_DEMO_DOMAIN}"
    address = str(TypeAdapter(EmailStr).validate_python(base)).lower()
    local, _, domain = address.partition("@")
    local = local.split("+", 1)[0]
    return f"{local}+demo-{label}@{domain}"


def demo_password() -> str:
    """A random password that satisfies the registration policy (upper, lower, digit, symbol)."""
    return f"Demo-{secrets.token_urlsafe(12)}-4k!"


async def ensure_user(name: str, email: str, role: UserRole, created: list[tuple[str, str, str, str]]) -> User:
    async with get_sessionmaker()() as session:
        existing = await session.scalar(select(User).where(func.lower(User.email) == email))
        if existing is not None:
            if existing.role != role:
                raise SystemExit(f"{email} already belongs to a {existing.role} account; change DEMO_EMAIL_BASE.")
            return existing
        password = demo_password()
        user = User(
            name=name,
            email=email,
            email_verified_at=func.now(),
            password_hash=hash_password(password),
            role=role,
            must_change_password=False,
            password_changed_at=func.now(),
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        created.append((role.value, name, email, password))
        return user


async def latest_prices() -> list[tuple[str, str, Decimal]]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(text(LATEST_PRICES_SQL))).all()
    return [(row.market, row.crop, Decimal(row.avg_price_per_kg)) for row in rows]


async def active_listing_count(farmers: list[User]) -> int:
    async with get_sessionmaker()() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(ProduceListing)
            .where(
                ProduceListing.farmer_id.in_([farmer.id for farmer in farmers]),
                ProduceListing.status == ListingStatus.ACTIVE,
            )
        )
    return int(count or 0)


async def create_listings(farmers: list[User], prices: list[tuple[str, str, Decimal]]) -> list[str]:
    summary = []
    for index, (quantity, factor) in enumerate(LISTING_PLAN):
        market, crop, price = prices[index % len(prices)]
        farmer = farmers[index % len(farmers)]
        unit_price = max(price * factor, Decimal("1")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        values = {
            "crop": crop,
            "market": market,
            "quantity_available": str(quantity),
            "unit": "kg",
            "price_per_unit": str(unit_price),
        }
        async with get_sessionmaker()() as session:
            await listing_service.create_listing(session, farmer, values, None)
        summary.append(f"{farmer.name}: {crop} in {market}, {quantity} kg at KES {unit_price}/kg")
    return summary


async def seed() -> None:
    settings = get_settings()
    if settings.app_env.strip().lower() == "production":
        raise SystemExit("Refusing to seed demo data: APP_ENV is production.")
    prices = await latest_prices()
    if not prices:
        raise SystemExit("No maize or beans prices in market_prices yet. Load prices first, then run this again.")

    base = settings.demo_email_base
    created: list[tuple[str, str, str, str]] = []
    farmers = [
        await ensure_user(name, demo_email(label, base), UserRole.FARMER, created) for name, label in DEMO_FARMERS
    ]
    buyers = [await ensure_user(name, demo_email(label, base), UserRole.BUYER, created) for name, label in DEMO_BUYERS]

    existing = await active_listing_count(farmers)
    listings = [] if existing else await create_listings(farmers, prices)

    print(f"Demo data for APP_ENV={settings.app_env}")
    print(f"Users: {len(farmers)} farmers and {len(buyers)} buyers ({len(created)} created now).")
    if created:
        print("\nNew demo passwords, shown ONCE. Store them somewhere safe and never reuse them anywhere else:")
        for role, name, email, password in created:
            print(f"  {role:<6}  {name:<22}  {email}  {password}")
    if listings:
        print(f"\nCreated {len(listings)} listings:")
        for line in listings:
            print(f"  {line}")
    else:
        print(f"\nListings unchanged: the demo farmers already have {existing} active listings.")
    print(
        "\nSigning in as a demo account needs the emailed sign in code: use EMAIL_PROVIDER=console "
        "(the code is in .dev_mailbox/) or set DEMO_EMAIL_BASE to your own address for plus addresses."
    )
    print("Demo buyers enter an M-Pesa number when paying; the sandbox accepts the Daraja test number.")


if __name__ == "__main__":
    asyncio.run(seed())
