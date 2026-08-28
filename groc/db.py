"""SQLite storage for scraped flyer items.

Using a real database (instead of overwriting a CSV each run) means data
accumulates over time: every scrape upserts rows keyed on
(flyer_id, item_name, raw_price_text, postal_code, valid_from), so re-running
the scraper against the same flyer just refreshes `scraped_at` instead of
duplicating rows, while a new flyer (next week's prices) adds new ones.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Union

SCHEMA = """
CREATE TABLE IF NOT EXISTS flyer_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    merchant TEXT NOT NULL,
    flyer_id INTEGER NOT NULL,
    item_name TEXT NOT NULL,
    raw_price_text TEXT,
    price REAL,
    was_price REAL,
    unit_price REAL,
    unit_label TEXT,
    deal_quantity INTEGER,
    package_size TEXT,
    valid_from TEXT,
    valid_to TEXT,
    postal_code TEXT NOT NULL,
    scraped_at TEXT NOT NULL,
    UNIQUE(flyer_id, item_name, raw_price_text, postal_code, valid_from)
);
CREATE INDEX IF NOT EXISTS idx_flyer_items_item_name ON flyer_items(item_name);
CREATE INDEX IF NOT EXISTS idx_flyer_items_merchant ON flyer_items(merchant);
CREATE INDEX IF NOT EXISTS idx_flyer_items_postal_code ON flyer_items(postal_code);
"""

_UPSERT_SQL = """
INSERT INTO flyer_items (
    merchant, flyer_id, item_name, raw_price_text, price, was_price,
    unit_price, unit_label, deal_quantity, package_size,
    valid_from, valid_to, postal_code, scraped_at
) VALUES (
    :merchant, :flyer_id, :item_name, :raw_price_text, :price, :was_price,
    :unit_price, :unit_label, :deal_quantity, :package_size,
    :valid_from, :valid_to, :postal_code, :scraped_at
)
ON CONFLICT(flyer_id, item_name, raw_price_text, postal_code, valid_from)
DO UPDATE SET
    price = excluded.price,
    was_price = excluded.was_price,
    unit_price = excluded.unit_price,
    unit_label = excluded.unit_label,
    deal_quantity = excluded.deal_quantity,
    package_size = excluded.package_size,
    valid_to = excluded.valid_to,
    scraped_at = excluded.scraped_at;
"""


def connect(db_path: Union[str, Path]) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def upsert_items(conn: sqlite3.Connection, rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    with closing(conn.cursor()) as cur:
        cur.executemany(_UPSERT_SQL, rows)
    conn.commit()
    return len(rows)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
