"""Manual check that concurrent checkouts can never reserve the same stock twice.

Not part of pytest. It WRITES to the database in DATABASE_URL using committed transactions:
it creates one farmer, five buyers (names starting ``ZZ_TEST_``), and three listings, places
concurrent orders, then deletes the orders and listings and disables the six test users (users
cannot be deleted). Run it only against a disposable or development database:

    ALLOW_CONCURRENCY_CHECK=1 python -m scripts.check_checkout_concurrency
"""

import asyncio
import os
import secrets
import sys
from collections import Counter
from decimal import Decimal
from uuid import UUID

from sqlalchemy import delete, func, select, update

from app.core.config import get_settings
from app.core.errors import AppError
from app.core.security import hash_password
from app.db import get_engine, get_sessionmaker
from app.models.enums import CropType, ListingStatus, UserRole
from app.models.listing import ProduceListing
from app.models.order import Order
from app.models.user import User
from app.services import order_service

TEST_PREFIX = "ZZ_TEST_"


def refuse_unless_allowed() -> None:
    """Exit unless explicitly enabled and not pointed at production."""
    if os.environ.get("ALLOW_CONCURRENCY_CHECK") != "1":
        sys.exit("Refusing to run: set ALLOW_CONCURRENCY_CHECK=1 to confirm this script may write test data.")
    if get_settings().app_env.strip().lower() == "production":
        sys.exit("Refusing to run: APP_ENV is production.")


async def unique_phone() -> str:
    """Return an unused phone number in the valid 2547XXXXXXXX format."""
    async with get_sessionmaker()() as session:
        while True:
            phone = f"2547{secrets.randbelow(10**8):08d}"
            if await session.scalar(select(User.id).where(User.phone_number == phone)) is None:
                return phone


async def create_user(role: UserRole, label: str) -> User:
    user = User(
        name=f"{TEST_PREFIX}{label}_{secrets.token_hex(3)}",
        phone_number=await unique_phone(),
        password_hash=hash_password(secrets.token_urlsafe(32)),
        role=role,
    )
    async with get_sessionmaker()() as session:
        session.add(user)
        await session.commit()
    return user


async def create_listing(farmer: User, quantity: str) -> UUID:
    listing = ProduceListing(
        farmer_id=farmer.id,
        farmer_role=UserRole.FARMER,
        crop=CropType.MAIZE,
        market="Nairobi",
        quantity_available=Decimal(quantity),
        unit="kg",
        price_per_unit=Decimal("40.00"),
        status=ListingStatus.ACTIVE,
    )
    async with get_sessionmaker()() as session:
        session.add(listing)
        await session.commit()
    return listing.id


async def attempt_order(buyer: User, listing_id: UUID, quantity: str) -> str:
    """Run one checkout in its own session (own connection and transaction); return its outcome."""
    async with get_sessionmaker()() as session:
        try:
            await order_service.create_order(session, buyer, listing_id, Decimal(quantity))
        except AppError as exc:
            return exc.code
        except Exception as exc:  # reported as a failure by type only, never with details
            return f"error:{type(exc).__name__}"
    return "success"


async def listing_state(listing_id: UUID) -> tuple[Decimal, ListingStatus]:
    async with get_sessionmaker()() as session:
        listing = await session.get(ProduceListing, listing_id)
        assert listing is not None
        return listing.quantity_available, listing.status


def report(name: str, outcomes: list[str], expected: Counter[str], state: tuple, expected_state: tuple) -> bool:
    passed = Counter(outcomes) == expected and state == expected_state
    print(f"{'PASS' if passed else 'FAIL'}: {name}")
    print(f"  outcomes: {dict(Counter(outcomes))} (expected {dict(expected)})")
    quantity, listing_status = state
    expected_quantity, expected_status = expected_state
    print(
        f"  listing: quantity={quantity} status={listing_status.value} "
        f"(expected {expected_quantity} {expected_status.value})"
    )
    return passed


async def cleanup(listing_ids: list[UUID], user_ids: list[UUID]) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(delete(Order).where(Order.listing_id.in_(listing_ids)))
        await session.execute(delete(ProduceListing).where(ProduceListing.id.in_(listing_ids)))
        await session.execute(
            update(User).where(User.id.in_(user_ids)).values(is_disabled=True, disabled_at=func.now())
        )
        await session.commit()
    print(f"Cleanup: deleted test orders and listings; {len(user_ids)} disabled {TEST_PREFIX} users remain.")


async def main() -> int:
    refuse_unless_allowed()
    listing_ids: list[UUID] = []
    user_ids: list[UUID] = []
    results: list[bool] = []
    try:
        farmer = await create_user(UserRole.FARMER, "farmer")
        user_ids.append(farmer.id)
        buyers = [await create_user(UserRole.BUYER, f"buyer{index}") for index in range(1, 6)]
        user_ids.extend(buyer.id for buyer in buyers)

        first = await create_listing(farmer, "10.00")
        listing_ids.append(first)
        outcomes = await asyncio.gather(*(attempt_order(buyer, first, "4") for buyer in buyers))
        results.append(
            report(
                "five buyers order 4 kg of 10 kg",
                list(outcomes),
                Counter({"success": 2, "insufficient_stock": 3}),
                await listing_state(first),
                (Decimal("2.00"), ListingStatus.ACTIVE),
            )
        )

        second = await create_listing(farmer, "4.00")
        listing_ids.append(second)
        outcomes = await asyncio.gather(*(attempt_order(buyer, second, "4") for buyer in buyers[:3]))
        results.append(
            report(
                "three buyers order 4 kg of 4 kg",
                list(outcomes),
                Counter({"success": 1, "insufficient_stock": 2}),
                await listing_state(second),
                (Decimal("0.00"), ListingStatus.SOLD_OUT),
            )
        )

        third = await create_listing(farmer, "10.00")
        listing_ids.append(third)
        outcomes = await asyncio.gather(*(attempt_order(buyers[4], third, "1") for _ in range(5)))
        results.append(
            report(
                "one buyer submits five identical orders at once",
                list(outcomes),
                Counter({"success": 1, "pending_order_exists": 4}),
                await listing_state(third),
                (Decimal("9.00"), ListingStatus.ACTIVE),
            )
        )
    finally:
        if user_ids:
            await cleanup(listing_ids, user_ids)
        await get_engine().dispose()

    if all(results) and len(results) == 3:
        print("All scenarios passed.")
        return 0
    print("One or more scenarios failed.")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
