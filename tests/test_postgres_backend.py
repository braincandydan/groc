"""Backend-parity tests against a real Postgres instance.

Skipped by default -- these need an actual Postgres to connect to, unlike the
rest of the suite which runs entirely against SQLite ':memory:'. Point
GROC_TEST_POSTGRES_DSN at one to run them, e.g. locally via:

    docker run -d --name groc-test-pg -e POSTGRES_PASSWORD=groc \\
        -e POSTGRES_DB=groc -p 5544:5432 postgres:16
    GROC_TEST_POSTGRES_DSN=postgresql://postgres:groc@localhost:5544/groc pytest tests/test_postgres_backend.py

These exist because the Postgres path has already caught one real bug that
every SQLite-only test missed: SQLite's LIKE is case-insensitive by default,
Postgres's isn't, so a query like "chicken" matched real data on SQLite but
silently returned zero rows on Postgres until search.py explicitly
lowercased both sides. That's exactly the class of bug this file guards
against -- SQLite behavior that happens to work, not because the SQL is
actually backend-agnostic.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import pytest

from groc import db
from groc.chat import ask
from groc.search import best_by_merchant, search_items, top_deals
from groc.webapp import create_app

DSN = os.environ.get("GROC_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(not DSN, reason="set GROC_TEST_POSTGRES_DSN to run Postgres backend tests")


def _row(**overrides):
    base = {
        "merchant": "No Frills",
        "flyer_id": 1,
        "item_name": "Chicken Breast 1kg",
        "raw_price_text": "$4.99",
        "price": 4.99,
        "was_price": None,
        "unit_price": None,
        "unit_label": None,
        "deal_quantity": None,
        "package_size": "1kg",
        "valid_from": "2026-08-01",
        "valid_to": "2026-08-07",
        "postal_code": "M5V2H1",
        "scraped_at": "2026-08-01T00:00:00+00:00",
        "cutout_image_url": None,
        "category": "Groceries",
    }
    base.update(overrides)
    return base


@pytest.fixture
def pg_conn():
    conn = db.connect(DSN)
    db.init_db(conn)
    conn.execute("DELETE FROM flyer_items")
    conn.execute("DELETE FROM tracked_postal_codes")
    conn.execute("DELETE FROM ingest_tokens")
    conn.commit()
    yield conn
    conn.close()


def test_init_db_is_idempotent_against_postgres(pg_conn):
    db.init_db(pg_conn)  # second call should not raise
    assert db.upsert_items(pg_conn, [_row()]) == 1


def test_upsert_idempotent_on_rerun_against_postgres(pg_conn):
    db.upsert_items(pg_conn, [_row()])
    db.upsert_items(pg_conn, [_row(scraped_at="2026-08-02T00:00:00+00:00")])
    rows = pg_conn.execute("SELECT * FROM flyer_items").fetchall()
    assert len(rows) == 1
    assert rows[0]["scraped_at"] == "2026-08-02T00:00:00+00:00"


def test_search_items_is_case_insensitive_against_postgres(pg_conn):
    # The bug this whole file exists to catch: SQLite's LIKE is
    # case-insensitive by default, Postgres's is not.
    db.upsert_items(pg_conn, [_row(item_name="CHICKEN BREAST")])
    rows = search_items(pg_conn, "chicken breast")
    assert len(rows) == 1


def test_search_items_orders_cheapest_first_against_postgres(pg_conn):
    db.upsert_items(pg_conn, [
        _row(flyer_id=1, merchant="Metro", item_name="Chicken Breast", price=7.99),
        _row(flyer_id=2, merchant="No Frills", item_name="Chicken Breast", price=4.99),
    ])
    rows = search_items(pg_conn, "chicken")
    assert [r["merchant"] for r in rows] == ["No Frills", "Metro"]


def test_search_items_empty_query_lists_everything_against_postgres(pg_conn):
    db.upsert_items(pg_conn, [
        _row(flyer_id=1, item_name="Chicken Breast"),
        _row(flyer_id=2, item_name="Bananas", price=0.79),
    ])
    rows = search_items(pg_conn, "")
    assert len(rows) == 2


def test_top_deals_excludes_null_and_zero_price_against_postgres(pg_conn):
    db.upsert_items(pg_conn, [
        _row(flyer_id=1, merchant="Healthy Planet", item_name="Entire Line", price=None, raw_price_text=""),
        _row(flyer_id=2, merchant="Walmart", item_name="Subsidized Phone", price=0.0, raw_price_text="0.0"),
        _row(flyer_id=3, merchant="FreshCo", item_name="Bananas", price=0.79),
    ])
    rows = top_deals(pg_conn)
    assert [r["item_name"] for r in rows] == ["Bananas"]


def test_best_by_merchant_against_postgres(pg_conn):
    db.upsert_items(pg_conn, [
        _row(flyer_id=1, merchant="Metro", item_name="Chicken Breast", price=4.99),
        _row(flyer_id=2, merchant="Metro", item_name="Chicken Breast Value Pack", price=6.99),
        _row(flyer_id=3, merchant="No Frills", item_name="Chicken Breast", price=5.99),
    ])
    collapsed = best_by_merchant(search_items(pg_conn, "chicken"))
    assert len(collapsed) == 2


@dataclass
class _FakeTextBlock:
    text: str
    type: str = "text"


class _FakeMessages:
    def create(self, **kwargs):
        @dataclass
        class _FakeResponse:
            content: list
        return _FakeResponse(content=[_FakeTextBlock(text="test answer")])


class _FakeClient:
    messages = _FakeMessages()


def test_chat_ask_against_postgres(pg_conn):
    db.upsert_items(pg_conn, [_row(item_name="Chicken Breast", merchant="No Frills", price=4.99)])
    result = ask(pg_conn, "what's the best deal on chicken breast", client=_FakeClient())
    assert result.answer == "test answer"
    assert result.sources[0]["merchant"] == "No Frills"


def test_chat_ask_top_deals_fallback_against_postgres(pg_conn):
    db.upsert_items(pg_conn, [_row(item_name="Bananas", merchant="FreshCo", price=0.79)])
    result = ask(pg_conn, "what should I buy this week to save money?", client=_FakeClient())
    assert result.sources[0]["merchant"] == "FreshCo"


def test_webapp_search_endpoint_against_postgres():
    setup_conn = db.connect(DSN)
    db.init_db(setup_conn)
    setup_conn.execute("DELETE FROM flyer_items")
    setup_conn.commit()
    db.upsert_items(setup_conn, [_row(item_name="Chicken Breast", merchant="No Frills")])
    setup_conn.close()

    app = create_app(db_path=DSN, chat_client=_FakeClient())  # webapp opens its own connection per request
    resp = app.test_client().get("/api/search?q=chicken")
    assert resp.status_code == 200
    assert resp.get_json()["results"][0]["merchant"] == "No Frills"


def test_track_postal_code_on_conflict_do_nothing_against_postgres(pg_conn):
    # ON CONFLICT (postal_code) DO NOTHING -- worth verifying directly since
    # Postgres's ON CONFLICT syntax/behavior isn't guaranteed identical to
    # SQLite's just because the same SQL string parses on both.
    db.track_postal_code(pg_conn, "M5V2H1")
    db.track_postal_code(pg_conn, "M5V2H1")
    assert db.list_tracked_postal_codes(pg_conn) == ["M5V2H1"]


def test_track_postal_code_returns_true_only_once_against_postgres(pg_conn):
    # The RETURNING-based "was this newly inserted" check -- confirmed
    # working identically on SQLite before writing this, but worth locking
    # in against real Postgres given this session's history of SQL that
    # parses on both but behaves differently.
    assert db.track_postal_code(pg_conn, "M5V2H1") is True
    assert db.track_postal_code(pg_conn, "M5V2H1") is False


def test_mark_postal_code_scraped_against_postgres(pg_conn):
    db.track_postal_code(pg_conn, "M5V2H1")
    db.mark_postal_code_scraped(pg_conn, "M5V2H1", "2026-08-30T06:00:00+00:00")
    row = pg_conn.execute("SELECT * FROM tracked_postal_codes WHERE postal_code = 'M5V2H1'").fetchone()
    assert row["last_scraped_at"] == "2026-08-30T06:00:00+00:00"


def test_get_postal_code_scraped_at_against_postgres(pg_conn):
    db.track_postal_code(pg_conn, "M5V2H1")
    assert db.get_postal_code_scraped_at(pg_conn, "M5V2H1") is None

    db.mark_postal_code_scraped(pg_conn, "M5V2H1", "2026-08-30T06:00:00+00:00")
    assert db.get_postal_code_scraped_at(pg_conn, "M5V2H1") == "2026-08-30T06:00:00+00:00"


def test_ingest_token_issue_and_redeem_against_postgres(pg_conn):
    # Single-use + postal-code-bound behavior confirmed identical on
    # Postgres, not just assumed to work because the SQL string parses --
    # this session's established pattern given real SQLite/Postgres
    # divergence bugs found before (e.g. LIKE case-sensitivity).
    token = db.issue_ingest_token(pg_conn, "M5V2H1")
    assert db.redeem_ingest_token(pg_conn, token, "V1Y7M4") is False  # wrong postal code
    assert db.redeem_ingest_token(pg_conn, token, "M5V2H1") is True
    assert db.redeem_ingest_token(pg_conn, token, "M5V2H1") is False  # already used
