"""Cross-store search over scraped flyer items.

Phase 2, first cut: tokenized keyword matching against item_name (every query
word must appear, case-insensitive), ranked by unit price where available and
falling back to plain price otherwise, so differently-sized packages are
still comparable when unit pricing exists.
"""
from __future__ import annotations

import sqlite3
from typing import Optional


def search_items(
    conn: sqlite3.Connection,
    query: str,
    postal_code: Optional[str] = None,
    limit: int = 50,
) -> list[sqlite3.Row]:
    """Return matching flyer_items rows, cheapest-effective-price first.

    An empty/blank query means "no item-name filter" — e.g. just list every
    item for a postal code, so a client can fetch everything once and filter
    it further client-side.
    """
    tokens = query.strip().split()
    where = ["LOWER(item_name) LIKE ? ESCAPE '\\'"] * len(tokens)
    params: list = [_like_pattern(t) for t in tokens]

    if postal_code:
        where.append("postal_code = ?")
        params.append(postal_code)

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    sql = f"""
        SELECT * FROM flyer_items
        {where_sql}
        ORDER BY
            CASE WHEN COALESCE(unit_price, price) IS NULL THEN 1 ELSE 0 END,
            CASE WHEN unit_price IS NOT NULL THEN unit_price ELSE price END ASC,
            price ASC
        LIMIT ?
    """
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def top_deals(
    conn: sqlite3.Connection,
    postal_code: Optional[str] = None,
    limit: int = 30,
) -> list[sqlite3.Row]:
    """Return the overall cheapest priced items, no item-name filter.

    For questions that don't name a specific product (e.g. "what should I buy
    this week to save money") — there's nothing to keyword-match against, so
    this gives a grounded set of genuinely cheap items to reason about instead.

    Excludes price <= 0: Flipp categorizes flyers as a whole rather than
    per-item, so a big-box store's single "Groceries"-tagged flyer can still
    contain its electronics section — `$0` subsidized-phone rows were showing
    up as the "cheapest" items otherwise.
    """
    where = ["price IS NOT NULL", "price > 0"]
    params: list = []

    if postal_code:
        where.append("postal_code = ?")
        params.append(postal_code)

    sql = f"""
        SELECT * FROM flyer_items
        WHERE {' AND '.join(where)}
        ORDER BY
            CASE WHEN unit_price IS NOT NULL THEN unit_price ELSE price END ASC
        LIMIT ?
    """
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def best_by_merchant(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Collapse to one (the cheapest) row per merchant, preserving input order."""
    seen = set()
    result = []
    for row in rows:
        if row["merchant"] in seen:
            continue
        seen.add(row["merchant"])
        result.append(row)
    return result


def _like_pattern(token: str) -> str:
    # Matched against LOWER(item_name), so lowercase here too: SQLite's LIKE
    # happens to be case-insensitive by default but Postgres's isn't -- this
    # makes the match explicit and correct on both instead of relying on that
    # SQLite-specific default.
    escaped = token.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
