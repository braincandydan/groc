from dataclasses import dataclass

from groc import db
from groc.chat import ask, extract_keywords


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


@dataclass
class _FakeTextBlock:
    text: str
    type: str = "text"


class _FakeMessages:
    def __init__(self, reply_text: str):
        self.reply_text = reply_text
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs

        @dataclass
        class _FakeResponse:
            content: list

        return _FakeResponse(content=[_FakeTextBlock(text=self.reply_text)])


class _FakeClient:
    def __init__(self, reply_text: str = "Cheapest is No Frills at $4.99."):
        self.messages = _FakeMessages(reply_text)


def test_extract_keywords_strips_stopwords_and_punctuation():
    assert extract_keywords("What's the best deal on chicken breast?") == "chicken breast"


def test_extract_keywords_falls_back_to_original_if_all_stopwords():
    assert extract_keywords("what is the best") == "what is the best"


def test_ask_passes_retrieved_context_to_the_model():
    conn = _conn_with(_row(item_name="Chicken Breast 1kg", price=4.99, merchant="No Frills"))
    client = _FakeClient()

    result = ask(conn, "what's the best deal on chicken breast", client=client)

    assert result.answer == "Cheapest is No Frills at $4.99."
    assert len(result.sources) == 1
    assert result.sources[0]["merchant"] == "No Frills"
    prompt = client.messages.last_kwargs["messages"][0]["content"]
    assert "No Frills" in prompt
    assert "$4.99" in prompt
    assert "chicken breast" in prompt.lower()


def test_ask_tells_model_when_nothing_matches():
    conn = _conn_with(_row(item_name="Chicken Breast 1kg"))
    client = _FakeClient()

    ask(conn, "what's the best deal on durian", client=client)

    prompt = client.messages.last_kwargs["messages"][0]["content"]
    assert "No matching flyer items" in prompt


def test_ask_uses_system_prompt_grounding_instruction():
    conn = _conn_with(_row())
    client = _FakeClient()

    ask(conn, "chicken breast deals", client=client)

    assert "ONLY" in client.messages.last_kwargs["system"]


def test_ask_respects_postal_code_filter():
    conn = _conn_with(
        _row(flyer_id=1, postal_code="M5V2H1", item_name="Chicken Breast", merchant="No Frills"),
        _row(flyer_id=2, postal_code="V1Y7M4", item_name="Chicken Breast", merchant="Save-On-Foods"),
    )
    client = _FakeClient()

    ask(conn, "chicken breast", postal_code="V1Y7M4", client=client)

    prompt = client.messages.last_kwargs["messages"][0]["content"]
    assert "Save-On-Foods" in prompt
    assert "No Frills" not in prompt


def test_ask_returns_empty_string_if_no_text_block_in_response():
    conn = _conn_with(_row())

    class _NoTextMessages:
        def create(self, **kwargs):
            @dataclass
            class _FakeResponse:
                content: list

            return _FakeResponse(content=[])

    class _NoTextClient:
        messages = _NoTextMessages()

    assert ask(conn, "chicken breast", client=_NoTextClient()).answer == ""


def test_ask_returns_empty_sources_when_nothing_matches():
    conn = _conn_with(_row(item_name="Chicken Breast 1kg"))
    result = ask(conn, "durian", client=_FakeClient())
    assert result.sources == []


def test_ask_falls_back_to_top_deals_when_no_product_named():
    conn = _conn_with(
        _row(flyer_id=1, merchant="Metro", item_name="Steak", price=19.99),
        _row(flyer_id=2, merchant="No Frills", item_name="Bananas", price=0.79),
    )
    client = _FakeClient()

    result = ask(conn, "what should I buy this week to save money?", client=client)

    prompt = client.messages.last_kwargs["messages"][0]["content"]
    assert "Bananas" in prompt
    assert "Steak" in prompt
    assert [s["item_name"] for s in result.sources] == ["Bananas", "Steak"]


def test_ask_uses_item_search_when_product_is_named_even_with_filler_words():
    conn = _conn_with(
        _row(flyer_id=1, merchant="Metro", item_name="Steak", price=19.99),
        _row(flyer_id=2, merchant="No Frills", item_name="Chicken Breast", price=4.99),
    )
    client = _FakeClient()

    ask(conn, "suggest the best deal on chicken breast to save money", client=client)

    prompt = client.messages.last_kwargs["messages"][0]["content"]
    assert "Chicken Breast" in prompt
    assert "Steak" not in prompt
