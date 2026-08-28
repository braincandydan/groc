from groc import db


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


def test_upsert_inserts_rows():
    conn = db.connect(":memory:")
    db.init_db(conn)

    count = db.upsert_items(conn, [_row()])
    assert count == 1

    rows = conn.execute("SELECT * FROM flyer_items").fetchall()
    assert len(rows) == 1
    assert rows[0]["item_name"] == "Chicken Breast 1kg"


def test_upsert_is_idempotent_on_rerun():
    conn = db.connect(":memory:")
    db.init_db(conn)

    db.upsert_items(conn, [_row()])
    db.upsert_items(conn, [_row(scraped_at="2026-08-02T00:00:00+00:00")])

    rows = conn.execute("SELECT * FROM flyer_items").fetchall()
    assert len(rows) == 1
    assert rows[0]["scraped_at"] == "2026-08-02T00:00:00+00:00"


def test_new_flyer_id_creates_new_row():
    conn = db.connect(":memory:")
    db.init_db(conn)

    db.upsert_items(conn, [_row(flyer_id=1)])
    db.upsert_items(conn, [_row(flyer_id=2)])

    rows = conn.execute("SELECT * FROM flyer_items").fetchall()
    assert len(rows) == 2


def test_stores_cutout_image_url_and_category():
    conn = db.connect(":memory:")
    db.init_db(conn)

    db.upsert_items(conn, [_row(
        cutout_image_url="https://f.wishabi.net/page_items/123/456/extra_large.jpg",
        category="Groceries,Pharmacy",
    )])

    row = conn.execute("SELECT * FROM flyer_items").fetchone()
    assert row["cutout_image_url"] == "https://f.wishabi.net/page_items/123/456/extra_large.jpg"
    assert row["category"] == "Groceries,Pharmacy"


def test_init_db_migrates_a_database_predating_the_new_columns():
    # Simulate a database created before cutout_image_url/category existed --
    # init_db() should ALTER TABLE them in rather than erroring or silently
    # skipping the migration on a real (already-created) database file.
    conn = db.connect(":memory:")
    conn.executescript("""
        CREATE TABLE flyer_items (
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
    """)
    conn.commit()

    db.init_db(conn)  # should not raise, and should add the missing columns
    db.upsert_items(conn, [_row(cutout_image_url="https://example.com/x.jpg", category="Groceries")])

    row = conn.execute("SELECT * FROM flyer_items").fetchone()
    assert row["cutout_image_url"] == "https://example.com/x.jpg"
    assert row["category"] == "Groceries"


def test_init_db_is_idempotent_when_columns_already_exist():
    conn = db.connect(":memory:")
    db.init_db(conn)
    db.init_db(conn)  # should not raise on the second call
    db.upsert_items(conn, [_row()])
    assert conn.execute("SELECT COUNT(*) FROM flyer_items").fetchone()[0] == 1
