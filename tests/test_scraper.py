from groc import db
from groc.scraper import scrape_postal_code


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
