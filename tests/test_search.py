from groc import db
from groc.search import best_by_merchant, search_items, top_deals


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


def _conn_with(*rows):
    conn = db.connect(":memory:")
    db.init_db(conn)
    db.upsert_items(conn, list(rows))
    return conn


def test_search_matches_case_insensitive_substring():
    conn = _conn_with(_row(item_name="Chicken Breast 1kg"))
    rows = search_items(conn, "chicken")
    assert len(rows) == 1
    assert rows[0]["item_name"] == "Chicken Breast 1kg"


def test_search_requires_all_tokens():
    conn = _conn_with(
        _row(flyer_id=1, item_name="Chicken Breast 1kg"),
        _row(flyer_id=2, item_name="Chicken Wings"),
    )
    rows = search_items(conn, "chicken breast")
    assert [r["item_name"] for r in rows] == ["Chicken Breast 1kg"]


def test_search_orders_by_price_ascending():
    conn = _conn_with(
        _row(flyer_id=1, merchant="Metro", item_name="Chicken Breast", price=7.99),
        _row(flyer_id=2, merchant="No Frills", item_name="Chicken Breast", price=4.99),
    )
    rows = search_items(conn, "chicken")
    assert [r["merchant"] for r in rows] == ["No Frills", "Metro"]


def test_search_prefers_unit_price_over_plain_price():
    # Cheaper sticker price, but worse per-unit price once package size differs.
    conn = _conn_with(
        _row(flyer_id=1, merchant="Metro", item_name="Chicken Breast", price=5.00, unit_price=10.00, unit_label="kg"),
        _row(flyer_id=2, merchant="No Frills", item_name="Chicken Breast", price=6.00, unit_price=6.00, unit_label="kg"),
    )
    rows = search_items(conn, "chicken")
    assert [r["merchant"] for r in rows] == ["No Frills", "Metro"]


def test_search_postal_code_filter():
    conn = _conn_with(
        _row(flyer_id=1, postal_code="M5V2H1", item_name="Chicken Breast"),
        _row(flyer_id=2, postal_code="V1Y7M4", item_name="Chicken Breast"),
    )
    rows = search_items(conn, "chicken", postal_code="V1Y7M4")
    assert len(rows) == 1
    assert rows[0]["postal_code"] == "V1Y7M4"


def test_search_respects_limit():
    rows = [_row(flyer_id=i, item_name=f"Chicken Breast {i}") for i in range(5)]
    conn = _conn_with(*rows)
    result = search_items(conn, "chicken", limit=2)
    assert len(result) == 2


def test_search_offset_returns_next_page():
    rows = [_row(flyer_id=i, item_name=f"Chicken Breast {i}", price=float(i)) for i in range(5)]
    conn = _conn_with(*rows)
    page1 = search_items(conn, "chicken", limit=2, offset=0)
    page2 = search_items(conn, "chicken", limit=2, offset=2)
    assert [r["item_name"] for r in page1] == ["Chicken Breast 0", "Chicken Breast 1"]
    assert [r["item_name"] for r in page2] == ["Chicken Breast 2", "Chicken Breast 3"]


def test_search_pagination_covers_every_row_with_no_dupes_or_skips():
    # Tied prices could be ordered arbitrarily by the DB engine without a
    # stable tiebreaker, causing a row to be skipped or repeated across pages.
    rows = [_row(flyer_id=i, item_name=f"Chicken Breast {i}", price=4.99) for i in range(7)]
    conn = _conn_with(*rows)

    seen = []
    offset = 0
    page_size = 3
    while True:
        page = search_items(conn, "chicken", limit=page_size, offset=offset)
        seen.extend(r["item_name"] for r in page)
        if len(page) < page_size:
            break
        offset += page_size

    assert sorted(seen) == sorted(f"Chicken Breast {i}" for i in range(7))
    assert len(seen) == len(set(seen))


def test_search_special_characters_are_escaped():
    conn = _conn_with(_row(item_name="100% Whole Wheat Bread"))
    # A literal "%" in the query shouldn't act as a wildcard.
    rows = search_items(conn, "100%")
    assert len(rows) == 1


def test_best_by_merchant_keeps_cheapest_first_occurrence():
    conn = _conn_with(
        _row(flyer_id=1, merchant="Metro", item_name="Chicken Breast", price=4.99),
        _row(flyer_id=2, merchant="Metro", item_name="Chicken Breast Value Pack", price=6.99),
        _row(flyer_id=3, merchant="No Frills", item_name="Chicken Breast", price=5.99),
    )
    rows = search_items(conn, "chicken")
    collapsed = best_by_merchant(rows)
    assert len(collapsed) == 2
    assert collapsed[0]["merchant"] == "Metro"
    assert collapsed[0]["price"] == 4.99


def test_search_places_null_price_rows_last():
    conn = _conn_with(
        _row(flyer_id=1, merchant="Healthy Planet", item_name="Chicken Entire Line", price=None, raw_price_text=""),
        _row(flyer_id=2, merchant="No Frills", item_name="Chicken Breast", price=4.99),
    )
    rows = search_items(conn, "chicken")
    assert [r["merchant"] for r in rows] == ["No Frills", "Healthy Planet"]


def test_search_no_matches_returns_empty_list():
    conn = _conn_with(_row(item_name="Chicken Breast"))
    assert search_items(conn, "durian") == []


def test_search_empty_query_lists_everything_for_postal_code():
    conn = _conn_with(
        _row(flyer_id=1, postal_code="M5V2H1", item_name="Chicken Breast"),
        _row(flyer_id=2, postal_code="M5V2H1", item_name="Bananas", price=0.79),
        _row(flyer_id=3, postal_code="V1Y7M4", item_name="Apples", price=0.99),
    )
    rows = search_items(conn, "", postal_code="M5V2H1")
    assert {r["item_name"] for r in rows} == {"Chicken Breast", "Bananas"}


def test_top_deals_orders_cheapest_first_across_all_items():
    conn = _conn_with(
        _row(flyer_id=1, merchant="Metro", item_name="Steak", price=19.99),
        _row(flyer_id=2, merchant="No Frills", item_name="Bananas", price=0.79),
        _row(flyer_id=3, merchant="FreshCo", item_name="Bread", price=2.99),
    )
    rows = top_deals(conn)
    assert [r["item_name"] for r in rows] == ["Bananas", "Bread", "Steak"]


def test_top_deals_excludes_null_price_rows():
    conn = _conn_with(
        _row(flyer_id=1, merchant="Healthy Planet", item_name="Entire Line", price=None, raw_price_text=""),
        _row(flyer_id=2, merchant="No Frills", item_name="Bananas", price=0.79),
    )
    rows = top_deals(conn)
    assert [r["item_name"] for r in rows] == ["Bananas"]


def test_top_deals_excludes_zero_and_negative_price_rows():
    # e.g. Walmart's $0 subsidized-phone rows leaking in via a "Groceries"-
    # tagged flyer that also covers other departments (Flipp categorizes
    # whole flyers, not individual items).
    conn = _conn_with(
        _row(flyer_id=1, merchant="Walmart", item_name="Subsidized Phone", price=0.0, raw_price_text="0.0"),
        _row(flyer_id=2, merchant="No Frills", item_name="Bananas", price=0.79),
    )
    rows = top_deals(conn)
    assert [r["item_name"] for r in rows] == ["Bananas"]


def test_top_deals_respects_postal_code_filter():
    conn = _conn_with(
        _row(flyer_id=1, postal_code="M5V2H1", item_name="Bananas", price=0.79),
        _row(flyer_id=2, postal_code="V1Y7M4", item_name="Apples", price=0.99),
    )
    rows = top_deals(conn, postal_code="V1Y7M4")
    assert [r["item_name"] for r in rows] == ["Apples"]


def test_top_deals_respects_limit():
    rows = [_row(flyer_id=i, item_name=f"Item {i}", price=float(i)) for i in range(1, 6)]
    conn = _conn_with(*rows)
    result = top_deals(conn, limit=2)
    assert len(result) == 2
    assert [r["item_name"] for r in result] == ["Item 1", "Item 2"]
