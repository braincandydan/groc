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
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
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

CREATE TABLE IF NOT EXISTS tracked_postal_codes (
    postal_code TEXT PRIMARY KEY,
    first_requested_at TEXT NOT NULL,
    last_scraped_at TEXT
);

CREATE TABLE IF NOT EXISTS ingest_tokens (
    token TEXT PRIMARY KEY,
    postal_code TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
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

CREATE TABLE IF NOT EXISTS tracked_postal_codes (
    postal_code TEXT PRIMARY KEY,
    first_requested_at TEXT NOT NULL,
    last_scraped_at TEXT
);

CREATE TABLE IF NOT EXISTS ingest_tokens (
    token TEXT PRIMARY KEY,
    postal_code TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT
);
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


# Arbitrary fixed key for a Postgres advisory lock -- serializes concurrent
# migration attempts (see init_db() below). Any stable constant works; this
# one has no special meaning.
_MIGRATION_LOCK_KEY = 749201337


def init_db(conn) -> None:
    if isinstance(conn, _PgConnection):
        # _connect() (webapp.py) calls this on every request, not just once
        # -- fine normally since CREATE TABLE IF NOT EXISTS is a cheap no-op
        # once the schema exists. But on a database that's never had a given
        # table (e.g. right after this code first deploys), many concurrent
        # serverless invocations can all race to create it at the same
        # moment -- confirmed via a real concurrency test that plain
        # "IF NOT EXISTS" isn't sufficient protection here: Postgres raised a
        # genuine DeadlockDetected under concurrent CREATE TABLE, not just
        # clean contention, and every request caught in it 500'd. A
        # transaction-scoped advisory lock serializes migration attempts
        # instead: everyone else just waits briefly for the lock rather than
        # racing on the same DDL. Auto-released by the commit() below --
        # nothing to explicitly unlock.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MIGRATION_LOCK_KEY,))
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


_TRACK_POSTAL_CODE_SQL = """
INSERT INTO tracked_postal_codes (postal_code, first_requested_at, last_scraped_at)
VALUES (:postal_code, :first_requested_at, NULL)
ON CONFLICT (postal_code) DO NOTHING
RETURNING postal_code;
"""


def track_postal_code(conn, postal_code: str) -> bool:
    """Record that a postal code has been requested, so a scheduled job can pick it up.

    A no-op if it's already tracked -- this is called on every search, not just
    the first time, so it has to stay cheap and idempotent. Returns True only
    when this postal code was newly recorded (not already tracked), so a
    caller can decide whether to kick off an immediate scrape instead of
    waiting for the next scheduled run -- verified identical on SQLite and
    Postgres via INSERT ... ON CONFLICT DO NOTHING RETURNING, which is a more
    reliable way to detect "did this insert actually happen" across both
    backends than relying on cursor.rowcount semantics.
    """
    cur = conn.execute(_TRACK_POSTAL_CODE_SQL, {"postal_code": postal_code, "first_requested_at": utcnow_iso()})
    inserted = cur.fetchone() is not None
    conn.commit()
    return inserted


def get_postal_code_scraped_at(conn, postal_code: str) -> Union[str, None]:
    """Returns last_scraped_at for a tracked postal code, or None if it's
    never been scraped (including if it isn't tracked at all).

    Lets a caller distinguish "haven't successfully checked this postal code
    yet" from "checked, and there's genuinely nothing here" -- both produce
    zero rows, but only the first should trigger another scrape attempt.
    """
    row = conn.execute(
        "SELECT last_scraped_at FROM tracked_postal_codes WHERE postal_code = ?", (postal_code,)
    ).fetchone()
    return row["last_scraped_at"] if row else None


def list_tracked_postal_codes(conn) -> list[str]:
    rows = conn.execute("SELECT postal_code FROM tracked_postal_codes ORDER BY postal_code").fetchall()
    return [row["postal_code"] for row in rows]


def mark_postal_code_scraped(conn, postal_code: str, scraped_at: str) -> None:
    conn.execute(
        "UPDATE tracked_postal_codes SET last_scraped_at = ? WHERE postal_code = ?",
        (scraped_at, postal_code),
    )
    conn.commit()


_INGEST_TOKEN_TTL_SECONDS = 600  # generous: even a dense-city scrape measured ~40s end-to-end


def issue_ingest_token(conn, postal_code: str) -> str:
    """Issue a short-lived, single-use token authorizing one /api/ingest-scrape
    submission for this exact postal code.

    Without this, /api/ingest-scrape would accept a shape-valid submission
    from *any* caller for *any* postal code -- there was nothing tying a
    submission back to an actual client-side scrape that /api/search itself
    kicked off. A token is only ever issued when a search reports a postal
    code as not-yet-scraped, is bound to that one postal code, expires
    quickly, and is consumed on first successful redemption.
    """
    # Opportunistic cleanup -- keeps the table bounded without a separate
    # cron job; cheap since it only touches already-expired rows.
    conn.execute("DELETE FROM ingest_tokens WHERE expires_at < ?", (utcnow_iso(),))
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=_INGEST_TOKEN_TTL_SECONDS)).isoformat()
    conn.execute(
        "INSERT INTO ingest_tokens (token, postal_code, expires_at, used_at) VALUES (?, ?, ?, NULL)",
        (token, postal_code, expires_at),
    )
    conn.commit()
    return token


def redeem_ingest_token(conn, token: str, postal_code: str) -> bool:
    """Consumes a token, returning True only if it's valid: exists, matches
    this postal code, isn't expired, and hasn't already been used. Single-use
    -- a second redemption attempt with the same token always fails, so a
    captured/replayed token can't be used to submit twice.
    """
    row = conn.execute(
        "SELECT postal_code, expires_at, used_at FROM ingest_tokens WHERE token = ?", (token,)
    ).fetchone()
    if row is None or row["used_at"] is not None or row["postal_code"] != postal_code:
        return False
    if row["expires_at"] < utcnow_iso():
        return False
    conn.execute("UPDATE ingest_tokens SET used_at = ? WHERE token = ?", (utcnow_iso(), token))
    conn.commit()
    return True


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
