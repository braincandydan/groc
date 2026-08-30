"""Storage for scraped flyer items -- SQLite locally, Postgres in production.

Using a real database (instead of overwriting a CSV each run) means data
accumulates over time: every scrape upserts rows keyed on
(flyer_id, item_name, raw_price_text, postal_code, valid_from), so re-running
the scraper against the same flyer just refreshes `scraped_at` instead of
duplicating rows, while a new flyer (next week's prices) adds new ones.

connect() picks the backend from `db_path`: a `postgres://`/`postgresql://`
URL goes to Postgres (e.g. Vercel/Neon in production, since serverless
deployments have no persistent local disk for a SQLite file); anything else
is treated as a SQLite file path (the default for local dev/CLI use).
Everywhere else in the codebase (scraper.py, search.py, chat.py, webapp.py)
only ever calls connect()/init_db()/upsert_items() and does
`conn.execute(...).fetchall()` / `row["col"]`, so it works unmodified against
either backend -- the Postgres connection is wrapped in a small adapter that
speaks the same shape and translates SQLite's `?`/`:name` placeholders to
Postgres's `%s`/`%(name)s` at the boundary.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Union

_SQLITE_SCHEMA = """
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
    cutout_image_url TEXT,
    category TEXT,
    UNIQUE(flyer_id, item_name, raw_price_text, postal_code, valid_from)
);
CREATE INDEX IF NOT EXISTS idx_flyer_items_item_name ON flyer_items(item_name);
CREATE INDEX IF NOT EXISTS idx_flyer_items_merchant ON flyer_items(merchant);
CREATE INDEX IF NOT EXISTS idx_flyer_items_postal_code ON flyer_items(postal_code);
"""

# Same shape as _SQLITE_SCHEMA -- only the primary key syntax differs
# (SQLite's AUTOINCREMENT vs Postgres's SERIAL). Everything else (column
# types, the UNIQUE constraint, indexes) is standard SQL both support.
_POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS flyer_items (
    id SERIAL PRIMARY KEY,
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
    cutout_image_url TEXT,
    category TEXT,
    UNIQUE(flyer_id, item_name, raw_price_text, postal_code, valid_from)
);
CREATE INDEX IF NOT EXISTS idx_flyer_items_item_name ON flyer_items(item_name);
CREATE INDEX IF NOT EXISTS idx_flyer_items_merchant ON flyer_items(merchant);
CREATE INDEX IF NOT EXISTS idx_flyer_items_postal_code ON flyer_items(postal_code);
"""

# Columns added after the initial release. init_db() ALTER TABLEs these into
# any pre-existing database that predates them, so an older DB upgrades in
# place instead of needing a manual migration.
_ADDED_COLUMNS = [
    ("cutout_image_url", "TEXT"),
    ("category", "TEXT"),
]

_UPSERT_SQL = """
INSERT INTO flyer_items (
    merchant, flyer_id, item_name, raw_price_text, price, was_price,
    unit_price, unit_label, deal_quantity, package_size,
    valid_from, valid_to, postal_code, scraped_at,
    cutout_image_url, category
) VALUES (
    :merchant, :flyer_id, :item_name, :raw_price_text, :price, :was_price,
    :unit_price, :unit_label, :deal_quantity, :package_size,
    :valid_from, :valid_to, :postal_code, :scraped_at,
    :cutout_image_url, :category
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
    scraped_at = excluded.scraped_at,
    cutout_image_url = excluded.cutout_image_url,
    category = excluded.category;
"""


def _is_postgres_dsn(db_path: Any) -> bool:
    return isinstance(db_path, str) and db_path.startswith(("postgres://", "postgresql://"))


# ---------------------------------------------------------------------------
# Postgres adapter: makes a psycopg connection look like a sqlite3.Connection
# for the subset of the API the rest of this codebase actually uses, so no
# other module needs to know which backend it's talking to.
# ---------------------------------------------------------------------------

_NAMED_PARAM_RE = re.compile(r":(\w+)")


def _translate_sql(sql: str, params: Any) -> tuple[str, Any]:
    """SQLite -> Postgres placeholder translation: ':name' -> '%(name)s', '?' -> '%s'."""
    if isinstance(params, dict):
        return _NAMED_PARAM_RE.sub(r"%(\1)s", sql), params
    return sql.replace("?", "%s"), params


class _PgCursor:
    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql: str, params: Any = None) -> "_PgCursor":
        sql2, params2 = _translate_sql(sql, params)
        self._cur.execute(sql2, params2)
        return self

    def executemany(self, sql: str, seq_of_params: Iterable[dict]) -> "_PgCursor":
        seq = list(seq_of_params)
        if seq:
            sql2, _ = _translate_sql(sql, seq[0])
            self._cur.executemany(sql2, seq)
        return self

    def fetchall(self):
        return self._cur.fetchall()

    def fetchone(self):
        return self._cur.fetchone()

    def close(self) -> None:
        self._cur.close()


class _PgConnection:
    """Adapts a psycopg connection to the sqlite3.Connection calls this codebase uses."""

    def __init__(self, raw_conn):
        from psycopg.rows import dict_row

        self._raw = raw_conn
        self._row_factory = dict_row

    def cursor(self) -> _PgCursor:
        return _PgCursor(self._raw.cursor(row_factory=self._row_factory))

    def execute(self, sql: str, params: Any = None) -> _PgCursor:
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def executescript(self, script: str) -> None:
        with closing(self._raw.cursor()) as cur:
            for statement in script.split(";"):
                statement = statement.strip()
                if statement:
                    cur.execute(statement)

    def commit(self) -> None:
        self._raw.commit()

    def close(self) -> None:
        self._raw.close()


def connect(db_path: Union[str, Path]):
    if _is_postgres_dsn(db_path):
        import psycopg

        return _PgConnection(psycopg.connect(db_path, autocommit=False))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn) -> None:
    if isinstance(conn, _PgConnection):
        conn.executescript(_POSTGRES_SCHEMA)
        for column, coltype in _ADDED_COLUMNS:
            conn.execute(f"ALTER TABLE flyer_items ADD COLUMN IF NOT EXISTS {column} {coltype}")
        conn.commit()
        return

    conn.executescript(_SQLITE_SCHEMA)
    for column, coltype in _ADDED_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE flyer_items ADD COLUMN {column} {coltype}")
        except sqlite3.OperationalError as e:
            if "duplicate column name" not in str(e):
                raise
    conn.commit()


def upsert_items(conn, rows: Iterable[dict]) -> int:
    rows = list(rows)
    if not rows:
        return 0
    with closing(conn.cursor()) as cur:
        cur.executemany(_UPSERT_SQL, rows)
    conn.commit()
    return len(rows)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
