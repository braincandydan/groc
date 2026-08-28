from groc.price_parser import parse_price


def test_plain_dollar_price():
    result = parse_price("$4.99")
    assert result.price == 4.99
    assert result.was_price is None
    assert result.deal_quantity is None


def test_plain_price_without_dollar_sign():
    result = parse_price("4.99")
    assert result.price == 4.99


def test_was_now_markdown():
    result = parse_price("was $6.99 now $4.99")
    assert result.was_price == 6.99
    assert result.price == 4.99


def test_multi_buy_for():
    result = parse_price("2 for $5.00")
    assert result.deal_quantity == 2
    assert result.price == 2.5


def test_multi_buy_slash():
    result = parse_price("3/$10")
    assert result.deal_quantity == 3
    assert result.price == round(10 / 3, 2)


def test_bogo():
    result = parse_price("Buy 1 Get 1 Free $4.00")
    assert result.deal_quantity == 2
    assert result.price == 2.0


def test_cents_only():
    result = parse_price("99¢")
    assert result.price == 0.99


def test_per_unit_pricing():
    result = parse_price("$1.99/100g")
    assert result.unit_price == 1.99
    assert result.unit_label == "100g"
    assert result.price == 1.99


def test_package_size_from_item_name():
    result = parse_price("$4.99", item_name="No Name Chicken Breast 1kg")
    assert result.package_size == "1kg"


def test_package_size_with_multiplier():
    result = parse_price("$5.99", item_name="Water 6x355mL")
    assert result.package_size == "355x6ml"


def test_empty_price_text_returns_none_price():
    result = parse_price("", item_name="Bananas")
    assert result.price is None
    assert result.raw_price_text == ""


def test_none_price_text_is_handled():
    result = parse_price(None)
    assert result.price is None
    assert result.raw_price_text == ""
