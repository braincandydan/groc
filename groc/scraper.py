"""Orchestrates fetching flyers/items from Flipp and storing them in the DB."""
from __future__ import annotations

import logging
from typing import Any, Iterable, Optional, Sequence

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
    # Defensive: flyer data used to only ever come from a trusted server-side
    # Flipp response, but now can also arrive as client-submitted JSON via the
    # /api/ingest-scrape endpoint (browser scrapes Flipp directly, then POSTs
    # the raw payload here to be parsed/stored) -- a malformed or malicious
    # `categories` value (e.g. a number, null) must not crash this, just be
    # treated as "no categories".
    if not isinstance(categories, (list, tuple, set)):
        return set()
    return {c for c in categories if isinstance(c, str) and c}


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


def _as_str_or_none(value: Any) -> Optional[str]:
    # Defensive coercion for fields that go straight into a DB column typed
    # TEXT: a trusted server-side Flipp response only ever has str/None here,
    # but client-submitted JSON (via /api/ingest-scrape) could contain
    # anything -- an unexpected type (a dict, a list) would otherwise reach
    # the DB adapter and raise, turning one bad row into a 500 for the whole
    # request. Dropping it to None is safer than guessing a stringification.
    if value is None or isinstance(value, str):
        return value
    return None


def _build_row(
    item: dict, merchant: str, flyer_id: int, postal_code: str, scraped_at: str, category: str = "",
) -> dict:
    name = str(item.get("name") or item.get("item_name") or "")
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
        "valid_from": _as_str_or_none(item.get("valid_from")),
        "valid_to": _as_str_or_none(item.get("valid_to")),
        "postal_code": postal_code,
        "scraped_at": scraped_at,
        "cutout_image_url": _as_str_or_none(item.get("cutout_image_url")),
        "category": category,
    }


def parse_and_store_flyer(
    conn,
    flyer: dict,
    items: Sequence[Any],
    postal_code: str,
    categories: Optional[set[str]] = DEFAULT_CATEGORIES,
) -> int:
    """Parse one already-fetched flyer's raw items and store them. Returns row count stored.

    This is the one place price/name parsing happens, shared by both the
    server-side scrape (`scrape_postal_code`, fed by a live FlippClient call)
    and the client-side ingest endpoint (fed by a browser's own raw fetch
    from Flipp, POSTed here as unparsed JSON). Never trust a client to parse
    its own prices/names -- that would let it fabricate arbitrary data
    straight into the DB. Every input here is treated as untrusted: the
    category filter, flyer_id, and every item are re-validated regardless of
    which path called this.
    """
    if categories and not (_flyer_categories(flyer) & categories):
        return 0

    try:
        flyer_id = int(flyer.get("id"))
    except (TypeError, ValueError):
        logger.warning("skipping flyer with invalid/missing id for postal_code=%s: %r", postal_code, flyer.get("id"))
        return 0

    merchant = str(flyer.get("merchant") or "Unknown")[:200]
    category = ",".join(sorted(_flyer_categories(flyer)))
    scraped_at = db.utcnow_iso()

    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            rows.append(_build_row(item, merchant, flyer_id, postal_code, scraped_at, category=category))
        except Exception:
            logger.warning("skipping malformed item for postal_code=%s flyer_id=%s", postal_code, flyer_id)
            continue

    stored = db.upsert_items(conn, rows)
    logger.info("merchant=%s flyer_id=%s stored %d items", merchant, flyer_id, stored)
    return stored


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

        # categories=None: `selected` is already category-filtered above, and
        # re-filtering here is harmless but redundant for this trusted path.
        total_rows += parse_and_store_flyer(conn, flyer, items, postal_code, categories=None)

    return total_rows


def run(postal_codes: Iterable[str], db_path: str, categories: Optional[set[str]] = DEFAULT_CATEGORIES) -> int:
    """Scrape a batch of postal codes end-to-end into the given database."""
    client = FlippClient()
    conn = db.connect(db_path)
    db.init_db(conn)
    total = 0
    try:
        for postal_code in postal_codes:
            total += scrape_postal_code(client, conn, postal_code, categories=categories)
            db.track_postal_code(conn, postal_code)
            db.mark_postal_code_scraped(conn, postal_code, db.utcnow_iso())
    finally:
        conn.close()
    return total


def run_tracked(db_path: str, categories: Optional[set[str]] = DEFAULT_CATEGORIES) -> dict[str, int]:
    """Re-scrape every postal code in tracked_postal_codes -- the scheduled-job entrypoint.

    Covers both "keep existing postal codes fresh" and "pick up a postal code
    someone searched for the first time" (the web app tracks every searched
    postal code via db.track_postal_code(), even ones with no data yet) with
    the same pass, since a never-scraped postal code and a stale one are
    handled identically: scrape it, record when.
    """
    client = FlippClient()
    conn = db.connect(db_path)
    db.init_db(conn)
    results: dict[str, int] = {}
    try:
        for postal_code in db.list_tracked_postal_codes(conn):
            results[postal_code] = scrape_postal_code(client, conn, postal_code, categories=categories)
            db.mark_postal_code_scraped(conn, postal_code, db.utcnow_iso())
    finally:
        conn.close()
    return results
