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
