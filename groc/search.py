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
    """Return matching flyer_items rows, cheapest-effective-price first."""
    tokens = query.strip().split()
    if not tokens:
        return []

    where = ["item_name LIKE ? ESCAPE '\\'"] * len(tokens)
    params: list = [_like_pattern(t) for t in tokens]

    if postal_code:
        where.append("postal_code = ?")
        params.append(postal_code)

    sql = f"""
        SELECT * FROM flyer_items
        WHERE {' AND '.join(where)}
        ORDER BY
            CASE WHEN COALESCE(unit_price, price) IS NULL THEN 1 ELSE 0 END,
            CASE WHEN unit_price IS NOT NULL THEN unit_price ELSE price END ASC,
            price ASC
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
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"
