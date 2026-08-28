"""Thin client around Flipp's public (unauthenticated) flyer API.

Flipp's app/site call this same backend. No API key is required, just a
random session id (sid) attached to each request.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

FLYERS_URL = "https://flyers-ng.flippback.com/api/flipp/data"
FLYER_ITEMS_URL = "https://flyers-ng.flippback.com/api/flipp/flyers/{flyer_id}/flyer_items"

DEFAULT_TIMEOUT = 20


def generate_sid() -> str:
    """Generate a random 16-digit session id. Doesn't need to be a "real" session."""
    return "".join(str(random.randint(0, 9)) for _ in range(16))


class FlippClient:
    """Fetches flyer listings and flyer items from Flipp's API."""

    def __init__(self, session: Optional[requests.Session] = None, request_delay: float = 0.5):
        self.session = session or requests.Session()
        self.request_delay = request_delay

    def _get(self, url: str, params: dict) -> Any:
        params = {**params, "sid": generate_sid()}
        response = self.session.get(url, params=params, timeout=DEFAULT_TIMEOUT)
        response.raise_for_status()
        if self.request_delay:
            time.sleep(self.request_delay)
        return response.json()

    def get_flyers(self, postal_code: str) -> list[dict]:
        """Return every flyer active for a postal code, across all merchants."""
        data = self._get(FLYERS_URL, {"locale": "en", "postal_code": postal_code})
        if isinstance(data, dict):
            return data.get("flyers") or []
        return []

    def get_flyer_items(self, flyer_id: int) -> list[dict]:
        """Return every item in a single flyer."""
        url = FLYER_ITEMS_URL.format(flyer_id=flyer_id)
        data = self._get(url, {"locale": "en"})
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("items") or data.get("flyer_items") or []
        return []
