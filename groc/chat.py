"""Chat layer: retrieval-grounded Q&A over scraped flyer deals via the Claude API.

Retrieval always runs before generation — the model is only ever shown rows
that actually came out of the database, and is instructed not to invent
prices/stores/products beyond that list.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from .search import search_items, top_deals

MODEL = "claude-opus-5"
SEARCH_LIMIT = 15

# Small hand-picked stopword list — good enough to turn a natural-language
# question into item-name search terms without pulling in an NLP dependency.
# Includes generic meal-planning/budget filler words (e.g. "save", "meal",
# "sale") on purpose: when everything left over is one of these, ask() takes
# that as a sign no specific product was named and falls back to top_deals()
# instead of keyword-searching item names for nonsense like "save money".
_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "what", "whats", "what's",
    "where", "which", "who", "when", "how", "best", "good", "cheap", "cheapest",
    "cheaper", "deal", "deals", "on", "for", "to", "of", "in", "at", "buy",
    "should", "i", "me", "my", "can", "you", "please", "find", "get", "some",
    "any", "this", "that", "that's", "week", "store", "stores", "and", "or",
    "with", "save", "money", "budget", "suggest", "recommend", "meal", "meals",
    "using", "stuff", "sale", "sales", "shopping", "list", "recipe", "recipes",
    "cook", "dinner", "something", "things", "thing", "ideas", "idea",
}


def _keyword_tokens(question: str) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9']+", question.lower())
    return [w for w in words if w not in _STOPWORDS]


def extract_keywords(question: str) -> str:
    """Strip punctuation/stopwords from a question, keeping the rest as search terms."""
    return " ".join(_keyword_tokens(question)) or question


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


@dataclass
class AskResult:
    """An answer plus the raw flyer rows it was grounded in, for source display."""
    answer: str
    sources: list = field(default_factory=list)


def ask(
    conn: sqlite3.Connection,
    question: str,
    *,
    postal_code: Optional[str] = None,
    search_limit: int = SEARCH_LIMIT,
    client=None,
    model: str = MODEL,
) -> AskResult:
    """Retrieve matching flyer items, then ask Claude to answer grounded in them.

    `client` accepts anything with a `.messages.create(...)` method shaped like
    the Anthropic SDK's response (a list of content blocks with `.type`/`.text`),
    so tests can pass a fake without hitting the network or importing `anthropic`.
    """
    if client is None:
        import anthropic

        client = anthropic.Anthropic()

    tokens = _keyword_tokens(question)
    if tokens:
        rows = search_items(conn, " ".join(tokens), postal_code=postal_code, limit=search_limit)
    else:
        # No specific product named (e.g. "what should I buy to save money") —
        # nothing sensible to keyword-match, so ground on the cheapest items instead.
        rows = top_deals(conn, postal_code=postal_code, limit=search_limit)
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
    answer = next((block.text for block in response.content if block.type == "text"), "")
    return AskResult(answer=answer, sources=[dict(row) for row in rows])
