"""Import selected FEWS Postman JSON price records into market_prices."""

import argparse
import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.db import SessionLocal

# TODO: map source JSON filenames to Nairobi, Kisumu, Mombasa, or Nakuru.
FILE_MARKET_MAP: dict[str, str] = {}
# TODO: choose the product and cpcv2 pair for each configured market/crop file.
PRODUCT_VARIANT_BY_MARKET: dict[str, dict[str, tuple[str, str]]] = {}


def records_from_json(payload: Any) -> list[dict[str, Any]]:
    """Extract record dictionaries from common FEWS export wrappers."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return [payload]
    return []


async def import_folder(folder: Path) -> None:
    """Validate and upsert configured FEWS records; mappings must be filled in first."""
    async with SessionLocal() as session:
        for file_path in sorted(folder.glob("*.json")):
            market = FILE_MARKET_MAP.get(file_path.name)
            if market is None:
                print(f"Warning: no market mapping for {file_path.name}; skipped.")
                continue
            records = records_from_json(json.loads(file_path.read_text(encoding="utf-8")))
            variants = {(str(row.get("product")), str(row.get("cpcv2"))) for row in records}
            if len(variants) > 1:
                print(f"Warning: {file_path.name} contains multiple (product, cpcv2) combinations.")
            for crop, chosen in PRODUCT_VARIANT_BY_MARKET.get(market, {}).items():
                for row in records:
                    if (str(row.get("product")), str(row.get("cpcv2"))) != chosen:
                        continue
                    if row.get("currency") != "KES" or row.get("common_unit") != "kg":
                        raise ValueError(f"Unexpected currency or unit in {file_path.name}")
                    month = date.fromisoformat(str(row["start_date"])[:10]).replace(day=1)
                    await session.execute(
                        text("""
                        INSERT INTO market_prices (market, crop, month, avg_price_per_kg, source)
                        VALUES (:market, :crop, :month, :price, 'fews')
                        ON CONFLICT (market, crop, month) DO UPDATE
                        SET avg_price_per_kg = EXCLUDED.avg_price_per_kg,
                            source = EXCLUDED.source,
                            fetched_at = now()
                    """),
                        {"market": market, "crop": crop, "month": month, "price": row["common_unit_price"]},
                    )
        await session.commit()


def main() -> None:
    """Parse the FEWS export directory and import its configured records."""
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    args = parser.parse_args()
    asyncio.run(import_folder(args.folder))


if __name__ == "__main__":
    main()
