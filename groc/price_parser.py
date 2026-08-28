"""Parsing for the messy, free-text prices Flipp flyer items come with.

Handles the common shapes seen in flyer data:
  - plain prices: "$4.99", "4.99"
  - cents-only prices: "99¢"
  - markdowns: "was $6.99 now $4.99"
  - multi-buy deals: "2 for $5.00", "3/$10"
  - buy-one-get-one deals: "buy 1 get 1 free"
  - per-unit pricing: "$1.99/100g", "$0.29/oz"

This is intentionally simple pattern matching, not a full NLP parser -
flyer copy varies a lot by store and this is meant to be improved
incrementally as more real-world formats show up.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

_MONEY = r"\$?\s*(\d+(?:\.\d{1,2})?)"
_DOLLAR_MONEY = r"\$\s*(\d+(?:\.\d{1,2})?)"

_WAS_NOW_RE = re.compile(rf"was\s*{_MONEY}.*?now\s*{_MONEY}", re.IGNORECASE)
_BOGO_RE = re.compile(r"buy\s*(\d+)\s*get\s*(\d+)\s*free", re.IGNORECASE)
# Negative lookbehind keeps this from matching the tail of a decimal (e.g.
# the "99" in "$1.99/100g") as if it were a "qty for/​/ total" deal.
_MULTI_BUY_RE = re.compile(rf"(?<![\d.])(\d+)\s*(?:for|/)\s*{_MONEY}", re.IGNORECASE)
_PER_UNIT_RE = re.compile(
    rf"{_MONEY}\s*/\s*(\d*\.?\d*\s?(?:g|kg|ml|l|lb|oz|ea|each))\b", re.IGNORECASE
)
_CENTS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:¢|c\b)", re.IGNORECASE)
_PLAIN_PRICE_RE = re.compile(_MONEY)
_PACKAGE_SIZE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(?:x\s*(\d+(?:\.\d+)?))?\s*(kg|g|ml|l|lb|oz|ct|pk|pack|pieces?)\b",
    re.IGNORECASE,
)


@dataclass
class ParsedPrice:
    raw_price_text: str
    price: Optional[float] = None
    was_price: Optional[float] = None
    unit_price: Optional[float] = None
    unit_label: Optional[str] = None
    deal_quantity: Optional[int] = None
    package_size: Optional[str] = None


def parse_price(raw_price_text: Optional[str], item_name: str = "") -> ParsedPrice:
    """Parse a flyer item's raw price text into structured fields.

    `item_name` is used as a fallback source for package size (e.g. item
    names like "No Name Chicken Breast 1kg" carry the size, not the price
    field).
    """
    text = (raw_price_text or "").strip()
    result = ParsedPrice(raw_price_text=text)

    if not text:
        result.package_size = _extract_package_size(item_name)
        return result

    match = _WAS_NOW_RE.search(text)
    if match:
        result.was_price = float(match.group(1))
        result.price = float(match.group(2))

    if result.price is None:
        match = _BOGO_RE.search(text)
        if match:
            total_units = int(match.group(1)) + int(match.group(2))
            result.deal_quantity = total_units
            plain = re.search(_DOLLAR_MONEY, text)
            if plain:
                result.price = round(float(plain.group(1)) / total_units, 2)

    if result.price is None:
        match = _MULTI_BUY_RE.search(text)
        if match:
            qty = int(match.group(1))
            total = float(match.group(2))
            if qty > 0:
                result.deal_quantity = qty
                result.price = round(total / qty, 2)

    match = _PER_UNIT_RE.search(text)
    if match:
        result.unit_price = float(match.group(1))
        result.unit_label = re.sub(r"\s+", "", match.group(2)).lower()

    if result.price is None:
        match = _CENTS_RE.search(text)
        if match:
            result.price = round(float(match.group(1)) / 100, 2)

    if result.price is None:
        match = _PLAIN_PRICE_RE.search(text)
        if match:
            result.price = float(match.group(1))

    result.package_size = _extract_package_size(text) or _extract_package_size(item_name)

    return result


def _extract_package_size(text: str) -> Optional[str]:
    if not text:
        return None
    match = _PACKAGE_SIZE_RE.search(text)
    if not match:
        return None
    qty, multiplier, unit = match.groups()
    unit = unit.lower()
    if multiplier:
        return f"{multiplier}x{qty}{unit}"
    return f"{qty}{unit}"
