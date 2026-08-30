"""Kicks off an immediate GitHub Actions scrape run for a newly-searched postal code.

A search request can't wait for a scrape itself (30-90+ seconds, far past
what a Vercel serverless function can run for), but it can ask the existing
scheduled workflow (.github/workflows/scrape.yml, workflow_dispatch-enabled)
to run right now instead of waiting for the next scheduled tick -- cutting a
new postal code's wait from "up to 24 hours" down to however long one
scrape-tracked run takes.
"""
from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger(__name__)

GITHUB_REPO = "braincandydan/groc"
WORKFLOW_FILE = "scrape.yml"
DISPATCH_URL = f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"


def trigger_scrape_now(postal_code: str) -> bool:
    """Best-effort only -- never raises. A failed trigger must not break the
    search request that caused it; the postal code stays tracked either way
    and the next scheduled run picks it up regardless.
    """
    token = os.environ.get("GH_DISPATCH_TOKEN")
    if not token:
        logger.info("GH_DISPATCH_TOKEN not set; skipping immediate scrape trigger for %s", postal_code)
        return False

    try:
        response = requests.post(
            DISPATCH_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            json={"ref": "main"},
            timeout=10,
        )
        if response.status_code >= 300:
            logger.warning(
                "GitHub workflow dispatch failed for %s: %s %s",
                postal_code, response.status_code, response.text,
            )
            return False
        logger.info("Triggered an immediate scrape run for new postal code %s", postal_code)
        return True
    except Exception:
        logger.exception("Error triggering GitHub workflow dispatch for %s", postal_code)
        return False
