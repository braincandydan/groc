import tempfile

from groc import db
from groc.scraper import parse_and_store_flyer, run_tracked, scrape_postal_code


class FakeFlippClient:
    def __init__(self, flyers, items_by_flyer):
        self._flyers = flyers
        self._items_by_flyer = items_by_flyer

    def get_flyers(self, postal_code):
        return self._flyers

    def get_flyer_items(self, flyer_id):
        return self._items_by_flyer.get(flyer_id, [])


def test_scrape_postal_code_stores_items_across_all_merchants():
    flyers = [
        {"id": 1, "merchant": "No Frills", "categories": ["Groceries"]},
        {"id": 2, "merchant": "Metro", "categories": ["Groceries"]},
        {"id": 3, "merchant": "Best Buy", "categories": ["Electronics"]},
    ]
    items_by_flyer = {
        1: [{"name": "Milk 2L", "price": "$3.99", "valid_from": "2026-08-01", "valid_to": "2026-08-07"}],
        2: [{"name": "Eggs Dozen", "price": "was $5.99 now $4.49", "valid_from": "2026-08-01", "valid_to": "2026-08-07"}],
        3: [{"name": "TV", "price": "$399.99"}],
    }
    client = FakeFlippClient(flyers, items_by_flyer)
    conn = db.connect(":memory:")
    db.init_db(conn)

    total = scrape_postal_code(client, conn, "M5V2H1")

    assert total == 2  # Best Buy filtered out by default category filter
    rows = conn.execute("SELECT * FROM flyer_items ORDER BY merchant").fetchall()
    merchants = {row["merchant"] for row in rows}
    assert merchants == {"No Frills", "Metro"}

    eggs = next(row for row in rows if row["merchant"] == "Metro")
    assert eggs["price"] == 4.49
    assert eggs["was_price"] == 5.99


def test_scrape_postal_code_all_categories():
    flyers = [{"id": 3, "merchant": "Best Buy", "categories": ["Electronics"]}]
    items_by_flyer = {3: [{"name": "TV", "price": "$399.99"}]}
    client = FakeFlippClient(flyers, items_by_flyer)
    conn = db.connect(":memory:")
    db.init_db(conn)

    total = scrape_postal_code(client, conn, "M5V2H1", categories=None)

    assert total == 1


def test_scrape_postal_code_captures_cutout_image_url_and_category():
    flyers = [{"id": 1, "merchant": "No Frills", "categories": ["Groceries", "Pharmacy"]}]
    items_by_flyer = {
        1: [{
            "name": "Milk 2L",
            "price": "$3.99",
            "valid_from": "2026-08-01",
            "valid_to": "2026-08-07",
            "cutout_image_url": "https://f.wishabi.net/page_items/123/456/extra_large.jpg",
        }],
    }
    client = FakeFlippClient(flyers, items_by_flyer)
    conn = db.connect(":memory:")
    db.init_db(conn)

    scrape_postal_code(client, conn, "M5V2H1")

    row = conn.execute("SELECT * FROM flyer_items").fetchone()
    assert row["cutout_image_url"] == "https://f.wishabi.net/page_items/123/456/extra_large.jpg"
    assert row["category"] == "Groceries,Pharmacy"


def test_scrape_postal_code_missing_cutout_image_url_stores_null():
    flyers = [{"id": 1, "merchant": "No Frills", "categories": ["Groceries"]}]
    items_by_flyer = {1: [{"name": "Milk 2L", "price": "$3.99"}]}
    client = FakeFlippClient(flyers, items_by_flyer)
    conn = db.connect(":memory:")
    db.init_db(conn)

    scrape_postal_code(client, conn, "M5V2H1")

    row = conn.execute("SELECT * FROM flyer_items").fetchone()
    assert row["cutout_image_url"] is None
    assert row["category"] == "Groceries"


def test_run_tracked_scrapes_every_tracked_postal_code(monkeypatch):
    flyers = [{"id": 1, "merchant": "No Frills", "categories": ["Groceries"]}]
    items_by_flyer = {1: [{"name": "Milk 2L", "price": "$3.99"}]}
    fake_client = FakeFlippClient(flyers, items_by_flyer)
    monkeypatch.setattr("groc.scraper.FlippClient", lambda: fake_client)

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    setup_conn = db.connect(tmp.name)
    db.init_db(setup_conn)
    db.track_postal_code(setup_conn, "M5V2H1")
    db.track_postal_code(setup_conn, "V1Y7M4")
    setup_conn.close()

    results = run_tracked(tmp.name)

    assert results == {"M5V2H1": 1, "V1Y7M4": 1}

    check_conn = db.connect(tmp.name)
    rows = check_conn.execute("SELECT * FROM tracked_postal_codes ORDER BY postal_code").fetchall()
    assert [r["postal_code"] for r in rows] == ["M5V2H1", "V1Y7M4"]
    assert all(r["last_scraped_at"] is not None for r in rows)


def test_run_tracked_with_no_tracked_postal_codes_does_nothing(monkeypatch):
    fake_client = FakeFlippClient([], {})
    monkeypatch.setattr("groc.scraper.FlippClient", lambda: fake_client)

    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()

    assert run_tracked(tmp.name) == {}


# ---------------------------------------------------------------------------
# parse_and_store_flyer -- the shared parse+store path used by both the
# server-side scrape above and the client-side ingest endpoint (a browser
# fetches raw Flipp JSON directly and POSTs it here to be parsed/stored, so
# every input has to be treated as untrusted regardless of which path it
# came from).
# ---------------------------------------------------------------------------

def test_parse_and_store_flyer_stores_items():
    conn = db.connect(":memory:")
    db.init_db(conn)
    flyer = {"id": 1, "merchant": "No Frills", "categories": ["Groceries"]}
    items = [{"name": "Milk 2L", "price": "$3.99"}]

    stored = parse_and_store_flyer(conn, flyer, items, "M5V2H1")

    assert stored == 1
    row = conn.execute("SELECT * FROM flyer_items").fetchone()
    assert row["merchant"] == "No Frills"
    assert row["price"] == 3.99


def test_parse_and_store_flyer_filters_by_category():
    conn = db.connect(":memory:")
    db.init_db(conn)
    flyer = {"id": 1, "merchant": "Best Buy", "categories": ["Electronics"]}
    items = [{"name": "TV", "price": "$399.99"}]

    stored = parse_and_store_flyer(conn, flyer, items, "M5V2H1")

    assert stored == 0
    assert conn.execute("SELECT COUNT(*) FROM flyer_items").fetchone()[0] == 0


def test_parse_and_store_flyer_all_categories_bypasses_filter():
    conn = db.connect(":memory:")
    db.init_db(conn)
    flyer = {"id": 1, "merchant": "Best Buy", "categories": ["Electronics"]}
    items = [{"name": "TV", "price": "$399.99"}]

    stored = parse_and_store_flyer(conn, flyer, items, "M5V2H1", categories=None)

    assert stored == 1


def test_parse_and_store_flyer_returns_zero_for_missing_flyer_id():
    conn = db.connect(":memory:")
    db.init_db(conn)
    flyer = {"merchant": "No Frills", "categories": ["Groceries"]}
    items = [{"name": "Milk 2L", "price": "$3.99"}]

    assert parse_and_store_flyer(conn, flyer, items, "M5V2H1") == 0


def test_parse_and_store_flyer_returns_zero_for_non_numeric_flyer_id():
    # A malicious/malformed client-submitted flyer id -- must be rejected
    # cleanly (skipped), never raise (that would surface as a 500 on the
    # ingest endpoint instead of a clean 400/partial success).
    conn = db.connect(":memory:")
    db.init_db(conn)
    flyer = {"id": "not-a-number", "merchant": "No Frills", "categories": ["Groceries"]}
    items = [{"name": "Milk 2L", "price": "$3.99"}]

    assert parse_and_store_flyer(conn, flyer, items, "M5V2H1") == 0


def test_parse_and_store_flyer_handles_garbage_categories_without_crashing():
    conn = db.connect(":memory:")
    db.init_db(conn)
    for bad_categories in (5, None, {"not": "a list"}, ["Groceries", 123, None]):
        flyer = {"id": 1, "merchant": "No Frills", "categories": bad_categories}
        # Should not raise. "Groceries" present in the last case still passes the filter.
        parse_and_store_flyer(conn, flyer, [{"name": "Milk", "price": "$3.99"}], "M5V2H1")


def test_parse_and_store_flyer_skips_malformed_items_without_crashing():
    conn = db.connect(":memory:")
    db.init_db(conn)
    flyer = {"id": 1, "merchant": "No Frills", "categories": ["Groceries"]}
    items = [
        {"name": "Milk 2L", "price": "$3.99"},
        "just a string, not a dict",
        None,
        42,
        {"name": "Eggs", "price": "$4.49"},
    ]

    stored = parse_and_store_flyer(conn, flyer, items, "M5V2H1")

    assert stored == 2
    names = {row["item_name"] for row in conn.execute("SELECT * FROM flyer_items").fetchall()}
    assert names == {"Milk 2L", "Eggs"}


def test_parse_and_store_flyer_coerces_non_string_cutout_image_url_to_null():
    conn = db.connect(":memory:")
    db.init_db(conn)
    flyer = {"id": 1, "merchant": "No Frills", "categories": ["Groceries"]}
    items = [{"name": "Milk 2L", "price": "$3.99", "cutout_image_url": {"unexpected": "shape"}}]

    parse_and_store_flyer(conn, flyer, items, "M5V2H1")

    row = conn.execute("SELECT * FROM flyer_items").fetchone()
    assert row["cutout_image_url"] is None
