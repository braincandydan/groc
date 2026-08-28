"""Orchestrates fetching flyers/items from Flipp and storing them in the DB."""
from __future__ import annotations

import logging
from typing import Iterable, Optional, Sequence

from . import db
from .flipp_client import FlippClient
from .price_parser import parse_price

logger = logging.getLogger(__name__)

# Flipp files most non-grocery flyers (electronics, pharmacy, home goods,
# etc.) under other categories. Default to "Groceries" only; pass
# categories=None to capture every merchant/category returned.
DEFAULT_CATEGORIES = {"Groceries"}


def _flyer_categories(flyer: dict) -> set[str]:
    categories = flyer.get("categories", [])
    if isinstance(categories, str):
        categories = [c.strip() for c in categories.split(",")]
    return {c for c in categories if c}


def _select_flyers(flyers: Sequence[dict], categories: Optional[set[str]]) -> list[dict]:
    if not categories:
        return list(flyers)
    return [f for f in flyers if _flyer_categories(f) & categories]


def _price_source_text(item: dict) -> str:
    # Different flyers populate different fields with the human-readable
    # price copy; take the first one that's actually set.
    for key in ("price_text", "pre_price_text", "sale_story", "price"):
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _build_row(
    item: dict, merchant: str, flyer_id: int, postal_code: str, scraped_at: str, category: str = "",
) -> dict:
    name = item.get("name") or item.get("item_name") or ""
    parsed = parse_price(_price_source_text(item), item_name=name)
    return {
        "merchant": merchant,
        "flyer_id": flyer_id,
        "item_name": name,
        "raw_price_text": parsed.raw_price_text,
        "price": parsed.price,
        "was_price": parsed.was_price,
        "unit_price": parsed.unit_price,
        "unit_label": parsed.unit_label,
        "deal_quantity": parsed.deal_quantity,
        "package_size": parsed.package_size,
        "valid_from": item.get("valid_from"),
        "valid_to": item.get("valid_to"),
        "postal_code": postal_code,
        "scraped_at": scraped_at,
        "cutout_image_url": item.get("cutout_image_url"),
        "category": category,
    }


def scrape_postal_code(
    client: FlippClient,
    conn,
    postal_code: str,
    categories: Optional[set[str]] = DEFAULT_CATEGORIES,
) -> int:
    """Fetch every flyer for a postal code and store its items. Returns row count stored."""
    flyers = client.get_flyers(postal_code)
    selected = _select_flyers(flyers, categories)
    logger.info(
        "postal_code=%s found %d flyers (%d after category filter)",
        postal_code, len(flyers), len(selected),
    )

    total_rows = 0
    for flyer in selected:
        flyer_id = flyer.get("id")
        merchant = flyer.get("merchant", "Unknown")
        if flyer_id is None:
            continue
        try:
            items = client.get_flyer_items(flyer_id)
        except Exception:
            logger.exception("failed to fetch items for flyer_id=%s merchant=%s", flyer_id, merchant)
            continue

        category = ",".join(sorted(_flyer_categories(flyer)))
        scraped_at = db.utcnow_iso()
        rows = [_build_row(item, merchant, flyer_id, postal_code, scraped_at, category=category) for item in items]
        total_rows += db.upsert_items(conn, rows)
        logger.info("merchant=%s flyer_id=%s stored %d items", merchant, flyer_id, len(rows))

    return total_rows


def run(postal_codes: Iterable[str], db_path: str, categories: Optional[set[str]] = DEFAULT_CATEGORIES) -> int:
    """Scrape a batch of postal codes end-to-end into the given SQLite database."""
    client = FlippClient()
    conn = db.connect(db_path)
    db.init_db(conn)
    total = 0
    try:
        for postal_code in postal_codes:
            total += scrape_postal_code(client, conn, postal_code, categories=categories)
    finally:
        conn.close()
    return total
