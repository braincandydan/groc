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
    data = resp.get_json()
    assert data["results"] == [] and data["has_more"] is False and data["postal_code_scraped"] is False
    assert data["ingest_token"]  # a fresh, unscraped postal code should get one


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


def test_api_search_reports_postal_code_not_yet_scraped(app_and_db):
    # Tells the frontend to kick off a client-side scrape (see
    # /api/ingest-scrape) rather than just showing an empty state -- this
    # postal code has been noted but nothing has actually fetched Flipp data
    # for it yet.
    client = app_and_db.test_client()
    resp = client.get("/api/search?postal_code=X1Y2Z3")
    data = resp.get_json()

    assert data["postal_code_scraped"] is False
    assert data["ingest_token"]  # only issued when a client-side scrape is actually the next step


def test_api_search_reports_postal_code_already_scraped(app_and_db):
    # Distinguishes "checked, genuinely nothing here" from "haven't checked
    # yet" -- the frontend must not keep re-triggering a client-side scrape
    # for an area that was already scraped and came back empty.
    conn = db.connect(app_and_db.config["DB_PATH"])
    db.track_postal_code(conn, "X1Y2Z3")
    db.mark_postal_code_scraped(conn, "X1Y2Z3", db.utcnow_iso())

    client = app_and_db.test_client()
    resp = client.get("/api/search?postal_code=X1Y2Z3")
    data = resp.get_json()

    assert data["postal_code_scraped"] is True
    assert data["ingest_token"] is None  # no scrape needed, so nothing to authorize


def test_api_search_omits_postal_code_scraped_semantics_without_a_postal_code(app_and_db):
    client = app_and_db.test_client()
    resp = client.get("/api/search?q=chicken")
    data = resp.get_json()

    assert data["postal_code_scraped"] is False
    assert data["ingest_token"] is None


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


# ---------------------------------------------------------------------------
# /api/ingest-scrape -- stores a client-side scrape's raw Flipp payload.
# This is a new attack surface (any browser can POST here), so most of these
# tests are about defensive input handling: garbage/oversized/malformed
# payloads must get a clean 400, never a 500, and a single bad flyer/item
# must not sink an otherwise-valid batch.
# ---------------------------------------------------------------------------

def _get_ingest_token(client, postal_code):
    # /api/ingest-scrape requires a token minted by a prior /api/search call
    # for this exact postal code -- see db.issue_ingest_token. Only issued
    # when that postal code hasn't been marked scraped yet.
    resp = client.get(f"/api/search?postal_code={postal_code}")
    token = resp.get_json()["ingest_token"]
    assert token, f"expected an ingest_token for postal_code={postal_code}"
    return token


def _ingest_payload(postal_code="N9Z9Z9", flyers=None, token=None):
    # A postal code distinct from the app_and_db fixture's seeded row
    # (M5V2H1) so assertions here can't collide with pre-existing fixture data.
    return {
        "postal_code": postal_code,
        "token": token,
        "flyers": flyers if flyers is not None else [
            {
                "flyer": {"id": 1, "merchant": "No Frills", "categories": ["Groceries"]},
                # valid_from/valid_to included -- real Flipp items always
                # have these, and upsert_items' dedup key includes valid_from
                # (NULL never conflicts with NULL in a UNIQUE constraint, so
                # omitting it would make every repeat ingest insert a new row
                # instead of updating the existing one).
                "items": [{"name": "Milk 2L", "price": "$3.99", "valid_from": "2026-08-24", "valid_to": "2026-08-30"}],
            },
        ],
    }


def test_api_ingest_scrape_rejects_a_submission_with_no_prior_search(app_and_db):
    # The core of the fix: a caller that never went through /api/search (so
    # never got a token) must not be able to store fabricated flyer data for
    # an arbitrary postal code.
    client = app_and_db.test_client()
    resp = client.post("/api/ingest-scrape", json=_ingest_payload(token=None))
    assert resp.status_code == 401

    resp = client.post("/api/ingest-scrape", json=_ingest_payload(token="totally-made-up-token"))
    assert resp.status_code == 401

    conn = db.connect(app_and_db.config["DB_PATH"])
    assert conn.execute("SELECT COUNT(*) FROM flyer_items WHERE postal_code = 'N9Z9Z9'").fetchone()[0] == 0


def test_api_ingest_scrape_rejects_a_token_issued_for_a_different_postal_code(app_and_db):
    client = app_and_db.test_client()
    token = _get_ingest_token(client, "N9Z9Z9")
    resp = client.post("/api/ingest-scrape", json=_ingest_payload(postal_code="X1Y2Z3", token=token))
    assert resp.status_code == 401


def test_api_ingest_scrape_rejects_a_reused_token(app_and_db):
    client = app_and_db.test_client()
    token = _get_ingest_token(client, "N9Z9Z9")
    first = client.post("/api/ingest-scrape", json=_ingest_payload(token=token))
    assert first.status_code == 200
    second = client.post("/api/ingest-scrape", json=_ingest_payload(token=token))
    assert second.status_code == 401


def test_api_ingest_scrape_stores_valid_payload(app_and_db):
    client = app_and_db.test_client()
    token = _get_ingest_token(client, "N9Z9Z9")
    resp = client.post("/api/ingest-scrape", json=_ingest_payload(token=token))

    assert resp.status_code == 200
    assert resp.get_json() == {"stored": 1}

    conn = db.connect(app_and_db.config["DB_PATH"])
    row = conn.execute("SELECT * FROM flyer_items WHERE postal_code = 'N9Z9Z9' AND merchant = 'No Frills'").fetchone()
    assert row["item_name"] == "Milk 2L"
    assert row["price"] == 3.99


def test_api_ingest_scrape_marks_the_postal_code_as_scraped(app_and_db):
    client = app_and_db.test_client()
    token = _get_ingest_token(client, "X1Y2Z3")
    client.post("/api/ingest-scrape", json=_ingest_payload(postal_code="X1Y2Z3", flyers=[], token=token))

    conn = db.connect(app_and_db.config["DB_PATH"])
    assert db.get_postal_code_scraped_at(conn, "X1Y2Z3") is not None


def test_api_ingest_scrape_filters_non_grocery_flyers(app_and_db):
    # A client (buggy or malicious) submitting a non-Groceries flyer must not
    # get it stored -- the server re-applies the category filter itself,
    # never trusting what the client claims should be included.
    client = app_and_db.test_client()
    token = _get_ingest_token(client, "N9Z9Z9")
    payload = _ingest_payload(token=token, flyers=[
        {"flyer": {"id": 1, "merchant": "Best Buy", "categories": ["Electronics"]}, "items": [{"name": "TV", "price": "$399.99"}]},
    ])
    resp = client.post("/api/ingest-scrape", json=payload)

    assert resp.status_code == 200
    assert resp.get_json() == {"stored": 0}
    conn = db.connect(app_and_db.config["DB_PATH"])
    assert conn.execute("SELECT COUNT(*) FROM flyer_items WHERE merchant = 'Best Buy'").fetchone()[0] == 0


def test_api_ingest_scrape_is_idempotent(app_and_db):
    # Two distinct tokens for the same not-yet-scraped postal code (both
    # fetched before either ingest call, since the first ingest marks it
    # scraped) -- submitting the same payload via each must still only store
    # one row, per upsert_items' existing dedup key.
    client = app_and_db.test_client()
    token1 = _get_ingest_token(client, "N9Z9Z9")
    token2 = _get_ingest_token(client, "N9Z9Z9")
    client.post("/api/ingest-scrape", json=_ingest_payload(token=token1))
    client.post("/api/ingest-scrape", json=_ingest_payload(token=token2))

    conn = db.connect(app_and_db.config["DB_PATH"])
    assert conn.execute("SELECT COUNT(*) FROM flyer_items WHERE postal_code = 'N9Z9Z9'").fetchone()[0] == 1


def test_api_ingest_scrape_skips_malformed_individual_items(app_and_db):
    client = app_and_db.test_client()
    token = _get_ingest_token(client, "N9Z9Z9")
    payload = _ingest_payload(token=token, flyers=[
        {
            "flyer": {"id": 1, "merchant": "No Frills", "categories": ["Groceries"]},
            "items": [{"name": "Milk 2L", "price": "$3.99"}, "not a dict", None, 42],
        },
    ])
    resp = client.post("/api/ingest-scrape", json=payload)

    assert resp.status_code == 200
    assert resp.get_json() == {"stored": 1}


def test_api_ingest_scrape_rejects_missing_postal_code(app_and_db):
    client = app_and_db.test_client()
    resp = client.post("/api/ingest-scrape", json=_ingest_payload(postal_code=""))
    assert resp.status_code == 400


def test_api_ingest_scrape_rejects_invalid_postal_code_format(app_and_db):
    client = app_and_db.test_client()
    resp = client.post("/api/ingest-scrape", json=_ingest_payload(postal_code="not-a-postal-code"))
    assert resp.status_code == 400


def test_api_ingest_scrape_rejects_non_list_flyers(app_and_db):
    client = app_and_db.test_client()
    token = _get_ingest_token(client, "M5V2H1")
    payload = {"postal_code": "M5V2H1", "token": token, "flyers": "not a list"}
    resp = client.post("/api/ingest-scrape", json=payload)
    assert resp.status_code == 400


def test_api_ingest_scrape_rejects_too_many_flyers(app_and_db):
    from groc.webapp import MAX_INGEST_FLYERS

    client = app_and_db.test_client()
    token = _get_ingest_token(client, "N9Z9Z9")
    flyers = [
        {"flyer": {"id": i, "merchant": "No Frills", "categories": ["Groceries"]}, "items": []}
        for i in range(MAX_INGEST_FLYERS + 1)
    ]
    resp = client.post("/api/ingest-scrape", json=_ingest_payload(token=token, flyers=flyers))
    assert resp.status_code == 400


def test_api_ingest_scrape_truncates_rather_than_rejects_an_oversized_flyer(app_and_db):
    # A real Walmart flyer for a dense Toronto FSA (M5V2H1) had 933 items --
    # rejecting the whole ~60-flyer submission over one legitimately large
    # flyer would silently discard everything else that was fine, so an
    # over-cap flyer is truncated instead of failing the whole request.
    from groc.webapp import MAX_INGEST_ITEMS_PER_FLYER

    client = app_and_db.test_client()
    token = _get_ingest_token(client, "N9Z9Z9")
    payload = _ingest_payload(token=token, flyers=[
        {
            "flyer": {"id": 1, "merchant": "No Frills", "categories": ["Groceries"]},
            "items": [
                {"name": f"Item {i}", "price": "$1.00", "valid_from": "2026-08-24", "valid_to": "2026-08-30"}
                for i in range(MAX_INGEST_ITEMS_PER_FLYER + 50)
            ],
        },
    ])
    resp = client.post("/api/ingest-scrape", json=payload)

    assert resp.status_code == 200
    assert resp.get_json() == {"stored": MAX_INGEST_ITEMS_PER_FLYER}


def test_api_ingest_scrape_rejects_entry_missing_flyer_object(app_and_db):
    client = app_and_db.test_client()
    token = _get_ingest_token(client, "N9Z9Z9")
    payload = _ingest_payload(token=token, flyers=[{"items": [{"name": "Milk", "price": "$3.99"}]}])
    resp = client.post("/api/ingest-scrape", json=payload)
    assert resp.status_code == 400


def test_api_ingest_scrape_rejects_entry_missing_items_list(app_and_db):
    client = app_and_db.test_client()
    token = _get_ingest_token(client, "N9Z9Z9")
    payload = _ingest_payload(token=token, flyers=[{"flyer": {"id": 1, "merchant": "No Frills", "categories": ["Groceries"]}}])
    resp = client.post("/api/ingest-scrape", json=payload)
    assert resp.status_code == 400


def test_api_ingest_scrape_rejects_non_json_body(app_and_db):
    client = app_and_db.test_client()
    resp = client.post("/api/ingest-scrape", data="not json", content_type="text/plain")
    assert resp.status_code == 400


def test_api_ingest_scrape_matches_what_server_side_scrape_would_produce(app_and_db):
    # Same raw flyer+items shape a real server-side scrape would have fed
    # through scrape_postal_code -- the client-ingest path must produce an
    # identical stored row via the same parse_and_store_flyer function.
    from groc.scraper import scrape_postal_code

    class _FakeClient:
        def get_flyers(self, postal_code):
            return [{"id": 99, "merchant": "Metro", "categories": ["Groceries"]}]

        def get_flyer_items(self, flyer_id):
            return [{"name": "Eggs Dozen", "price": "was $5.99 now $4.49"}]

    conn = db.connect(app_and_db.config["DB_PATH"])
    scrape_postal_code(_FakeClient(), conn, "V1Y7M4")
    server_row = dict(conn.execute("SELECT * FROM flyer_items WHERE postal_code = 'V1Y7M4'").fetchone())

    client = app_and_db.test_client()
    token = _get_ingest_token(client, "N9Z9Z9")
    payload = {
        "postal_code": "N9Z9Z9",
        "token": token,
        "flyers": [{
            "flyer": {"id": 99, "merchant": "Metro", "categories": ["Groceries"]},
            "items": [{"name": "Eggs Dozen", "price": "was $5.99 now $4.49"}],
        }],
    }
    client.post("/api/ingest-scrape", json=payload)
    client_row = dict(conn.execute("SELECT * FROM flyer_items WHERE postal_code = 'N9Z9Z9'").fetchone())

    for field in ("merchant", "item_name", "raw_price_text", "price", "was_price", "category"):
        assert server_row[field] == client_row[field]
