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
