import re
import tempfile
from dataclasses import dataclass

import pytest

from groc import db
from groc.webapp import create_app


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


@dataclass
class _FakeTextBlock:
    text: str
    type: str = "text"


class _FakeMessages:
    def __init__(self, reply_text: str):
        self.reply_text = reply_text

    def create(self, **kwargs):
        @dataclass
        class _FakeResponse:
            content: list

        return _FakeResponse(content=[_FakeTextBlock(text=self.reply_text)])


class _FakeChatClient:
    def __init__(self, reply_text: str = "Cheapest is No Frills at $4.99."):
        self.messages = _FakeMessages(reply_text)


@pytest.fixture
def app_and_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = db.connect(tmp.name)
    db.init_db(conn)
    db.upsert_items(conn, [_row()])
    conn.close()

    app = create_app(db_path=tmp.name, chat_client=_FakeChatClient())
    app.config["TESTING"] = True
    return app


def test_index_page_loads(app_and_db):
    client = app_and_db.test_client()
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"groc" in resp.data


def test_api_search_works_against_a_completely_fresh_unmigrated_database():
    # Regression: the web app used to only ever connect(), relying on some
    # CLI command (scrape/scrape-tracked) having already run init_db() at
    # some point. A schema change (tracked_postal_codes) 500'd in production
    # because nothing had migrated it yet. The app must self-migrate.
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()  # deliberately no db.init_db() call -- an empty, schema-less file

    app = create_app(db_path=tmp.name, chat_client=_FakeChatClient())
    app.config["TESTING"] = True
    resp = app.test_client().get("/api/search?postal_code=M5V2H1")

    assert resp.status_code == 200
    assert resp.get_json() == {"results": [], "has_more": False}


def test_api_search_returns_matches(app_and_db):
    client = app_and_db.test_client()
    resp = client.get("/api/search?q=chicken")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["results"]) == 1
    assert data["results"][0]["merchant"] == "No Frills"


def test_api_search_with_no_query_lists_everything_for_postal_code(app_and_db):
    conn = db.connect(app_and_db.config["DB_PATH"])
    db.upsert_items(conn, [_row(flyer_id=2, item_name="Bananas", price=0.79)])

    client = app_and_db.test_client()
    resp = client.get("/api/search?postal_code=M5V2H1")
    assert resp.status_code == 200
    names = {r["item_name"] for r in resp.get_json()["results"]}
    assert names == {"Chicken Breast 1kg", "Bananas"}


def test_api_search_postal_code_is_case_and_space_insensitive(app_and_db):
    client = app_and_db.test_client()
    # Row is stored as "M5V2H1"; the browser might send lowercase/spaced input.
    resp = client.get("/api/search?q=chicken&postal_code=m5v 2h1")
    data = resp.get_json()
    assert len(data["results"]) == 1
    assert data["results"][0]["merchant"] == "No Frills"


def test_api_search_offset_pagination(app_and_db):
    conn = db.connect(app_and_db.config["DB_PATH"])
    db.upsert_items(conn, [_row(flyer_id=i, item_name=f"Chicken Breast {i}", price=float(i)) for i in range(2, 6)])

    client = app_and_db.test_client()
    page1 = client.get("/api/search?q=chicken&limit=2&offset=0").get_json()
    page2 = client.get("/api/search?q=chicken&limit=2&offset=2").get_json()

    names1 = {r["item_name"] for r in page1["results"]}
    names2 = {r["item_name"] for r in page2["results"]}
    assert len(page1["results"]) == 2
    assert names1.isdisjoint(names2)


def test_api_search_has_more_flag(app_and_db):
    conn = db.connect(app_and_db.config["DB_PATH"])
    conn.execute("DELETE FROM flyer_items")  # app_and_db's fixture row also matches "chicken"; start from a known count
    conn.commit()
    db.upsert_items(conn, [_row(flyer_id=i, item_name=f"Chicken Breast {i}") for i in range(3)])

    client = app_and_db.test_client()
    full_page = client.get("/api/search?q=chicken&limit=2").get_json()
    last_page = client.get("/api/search?q=chicken&limit=2&offset=2").get_json()

    assert full_page["has_more"] is True
    assert last_page["has_more"] is False


def test_api_search_best_per_merchant(app_and_db):
    conn = db.connect(app_and_db.config["DB_PATH"])
    db.upsert_items(conn, [_row(flyer_id=2, merchant="No Frills", item_name="Chicken Breast Value Pack", price=9.99)])

    client = app_and_db.test_client()
    resp = client.get("/api/search?q=chicken&best_per_merchant=1")
    data = resp.get_json()
    assert len(data["results"]) == 1
    assert data["results"][0]["price"] == 4.99


def test_api_search_postal_code_filter(app_and_db):
    conn = db.connect(app_and_db.config["DB_PATH"])
    db.upsert_items(conn, [_row(flyer_id=2, postal_code="V1Y7M4", merchant="Save-On-Foods")])

    client = app_and_db.test_client()
    resp = client.get("/api/search?q=chicken&postal_code=V1Y7M4")
    data = resp.get_json()
    assert len(data["results"]) == 1
    assert data["results"][0]["merchant"] == "Save-On-Foods"


def test_api_search_tracks_the_postal_code_even_with_no_data(app_and_db):
    # So the scheduled scraper (groc scrape-tracked) picks up a postal code
    # someone searched for the first time, even though it has zero rows now.
    client = app_and_db.test_client()
    client.get("/api/search?postal_code=X1Y2Z3")

    conn = db.connect(app_and_db.config["DB_PATH"])
    assert "X1Y2Z3" in db.list_tracked_postal_codes(conn)


def test_api_search_triggers_immediate_scrape_only_on_first_search_of_a_postal_code(app_and_db, monkeypatch):
    calls = []
    monkeypatch.setattr("groc.webapp.trigger_scrape_now", lambda postal_code: calls.append(postal_code))

    client = app_and_db.test_client()
    client.get("/api/search?postal_code=X1Y2Z3")
    client.get("/api/search?postal_code=X1Y2Z3")  # searched again -- already tracked, shouldn't re-trigger
    client.get("/api/search?postal_code=X1Y2Z3")

    assert calls == ["X1Y2Z3"]


def test_api_search_does_not_trigger_for_an_already_tracked_postal_code(app_and_db, monkeypatch):
    conn = db.connect(app_and_db.config["DB_PATH"])
    db.track_postal_code(conn, "M5V2H1")  # pre-track it, simulating an earlier search

    calls = []
    monkeypatch.setattr("groc.webapp.trigger_scrape_now", lambda postal_code: calls.append(postal_code))

    client = app_and_db.test_client()
    client.get("/api/search?postal_code=M5V2H1")

    assert calls == []


def test_api_ask_returns_answer_and_sources(app_and_db):
    client = app_and_db.test_client()
    resp = client.post("/api/ask", json={"question": "what's the best deal on chicken breast"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["answer"] == "Cheapest is No Frills at $4.99."
    assert len(data["sources"]) == 1
    assert data["sources"][0]["merchant"] == "No Frills"


def test_api_ask_requires_question_field(app_and_db):
    client = app_and_db.test_client()
    resp = client.post("/api/ask", json={})
    assert resp.status_code == 400


def test_api_search_returns_json_error_on_unhandled_exception(app_and_db, monkeypatch):
    # A crash (e.g. a bad DB connection string) should never surface Flask's
    # default HTML error page to an API client -- that's what produced the
    # "Unexpected token '<'" bug when this endpoint 500'd on a real deployment.
    def _boom(*args, **kwargs):
        raise RuntimeError("password authentication failed for user 'secret_should_not_leak'")

    monkeypatch.setattr("groc.webapp.search_items", _boom)

    client = app_and_db.test_client()
    resp = client.get("/api/search?q=chicken")

    assert resp.status_code == 500
    assert resp.content_type == "application/json"
    data = resp.get_json()
    assert data == {"error": "internal server error"}
    assert "secret_should_not_leak" not in resp.get_data(as_text=True)


def test_favicon_returns_no_content(app_and_db):
    client = app_and_db.test_client()
    resp = client.get("/favicon.ico")
    assert resp.status_code == 204


def test_unknown_route_returns_plain_404_not_500(app_and_db):
    # HTTPExceptions (routine 404s) shouldn't be logged/treated as crashes --
    # this was misfiring as a 500 for every /favicon.ico request in production
    # before the errorhandler special-cased HTTPException.
    client = app_and_db.test_client()
    resp = client.get("/this-route-does-not-exist")
    assert resp.status_code == 404


def test_api_places_covers_more_than_a_curated_shortlist(app_and_db):
    # Regression: the city picker used to ship with only 115 hand-curated
    # cities, which left out most of the country (a real complaint -- towns
    # like "Zurich, ON" or "100 Mile House, BC" weren't findable at all).
    # This endpoint must serve the full GeoNames-derived list instead.
    client = app_and_db.test_client()
    resp = client.get("/api/places")

    assert resp.status_code == 200
    places = resp.get_json()
    assert len(places) > 1000

    by_name = {(name, province): postal for name, province, postal in places}
    assert by_name[("Toronto", "ON")] == "M3C 0C1"
    assert ("Zurich", "ON") in by_name  # a small town, not one of the old 115


def test_api_places_entries_look_like_real_postal_codes(app_and_db):
    client = app_and_db.test_client()
    places = client.get("/api/places").get_json()

    for name, province, postal in places[:50]:
        assert name and province
        assert re.match(r"^[A-Za-z]\d[A-Za-z] ?\d[A-Za-z]\d$", postal), postal
