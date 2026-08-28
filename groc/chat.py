"""Chat layer: retrieval-grounded Q&A over scraped flyer deals via the Claude API.

Retrieval always runs before generation — the model is only ever shown rows
that actually came out of the database, and is instructed not to invent
prices/stores/products beyond that list.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Optional

from .search import search_items

MODEL = "claude-opus-5"
SEARCH_LIMIT = 15

# Small hand-picked stopword list — good enough to turn a natural-language
# question into item-name search terms without pulling in an NLP dependency.
_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "what", "whats", "what's",
    "where", "which", "who", "when", "how", "best", "good", "cheap", "cheapest",
    "cheaper", "deal", "deals", "on", "for", "to", "of", "in", "at", "buy",
    "should", "i", "me", "my", "can", "you", "please", "find", "get", "some",
    "any", "this", "that", "week", "store", "stores", "and", "or", "with",
}


def extract_keywords(question: str) -> str:
    """Strip punctuation/stopwords from a question, keeping the rest as search terms."""
    words = re.findall(r"[a-zA-Z0-9']+", question.lower())
    keywords = [w for w in words if w not in _STOPWORDS]
    return " ".join(keywords) or question


SYSTEM_PROMPT = (
    "You are a grocery deal assistant. Answer the user's question using ONLY "
    "the flyer deals listed below — never invent a price, store, or product "
    "that isn't in the list. If nothing in the list answers the question, say "
    "so plainly rather than guessing. Keep answers short and concrete: name "
    "the store and price for anything you recommend."
)


def _format_context(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "No matching flyer items were found in the database."
    lines = []
    for row in rows:
        effective = row["unit_price"] if row["unit_price"] is not None else row["price"]
        price = f"${effective:.2f}" if effective is not None else "price not listed"
        lines.append(f"- {row['merchant']}: {row['item_name']} — {price} (valid to {row['valid_to']})")
    return "\n".join(lines)


def ask(
    conn: sqlite3.Connection,
    question: str,
    *,
    postal_code: Optional[str] = None,
    search_limit: int = SEARCH_LIMIT,
    client=None,
    model: str = MODEL,
) -> str:
    """Retrieve matching flyer items, then ask Claude to answer grounded in them.

    `client` accepts anything with a `.messages.create(...)` method shaped like
    the Anthropic SDK's response (a list of content blocks with `.type`/`.text`),
    so tests can pass a fake without hitting the network or importing `anthropic`.
    """
    if client is None:
        import anthropic

        client = anthropic.Anthropic()

    keywords = extract_keywords(question)
    rows = search_items(conn, keywords, postal_code=postal_code, limit=search_limit)
    context = _format_context(rows)

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Flyer deals:\n{context}\n\nQuestion: {question}",
        }],
    )
    return next((block.text for block in response.content if block.type == "text"), "")
