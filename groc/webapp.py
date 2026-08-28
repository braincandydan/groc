"""Minimal HTTP API + bare-bones HTML frontend over the search/chat layers.

Deliberately no visual design here — plain HTML and inline JS, functionality
only. The design pass is expected to happen later, directly on top of this.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from flask import Flask, jsonify, render_template, request

from . import db
from .chat import ask as chat_ask
from .search import best_by_merchant, search_items


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _normalize_postal_code(value: Optional[str]) -> Optional[str]:
    """Uppercase/strip so 'v1y7m4' matches stored 'V1Y7M4' rows instead of silently finding nothing."""
    if not value:
        return None
    cleaned = value.strip().upper().replace(" ", "")
    return cleaned or None


def create_app(db_path: str = "groc.db", chat_client=None) -> Flask:
    """Build the Flask app. `chat_client` is injectable so tests can avoid the network."""
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.config["CHAT_CLIENT"] = chat_client

    def _connect() -> sqlite3.Connection:
        return db.connect(app.config["DB_PATH"])

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/search")
    def api_search():
        # Blank/missing 'q' means "list everything for this postal code" —
        # the frontend uses this for an initial full load, then filters
        # client-side rather than re-querying per keystroke.
        query = request.args.get("q", "").strip()
        postal_code = _normalize_postal_code(request.args.get("postal_code"))
        best_per_merchant = request.args.get("best_per_merchant") in ("1", "true", "yes")
        try:
            limit = int(request.args.get("limit", 20))
        except ValueError:
            return jsonify({"error": "'limit' must be an integer"}), 400

        conn = _connect()
        rows = search_items(conn, query, postal_code=postal_code, limit=limit)
        if best_per_merchant:
            rows = best_by_merchant(rows)
        return jsonify({"results": [_row_to_dict(r) for r in rows]})

    @app.post("/api/ask")
    def api_ask():
        payload = request.get_json(silent=True) or {}
        question = (payload.get("question") or "").strip()
        if not question:
            return jsonify({"error": "missing required field 'question'"}), 400

        postal_code = _normalize_postal_code(payload.get("postal_code"))
        conn = _connect()
        result = chat_ask(conn, question, postal_code=postal_code, client=app.config["CHAT_CLIENT"])
        return jsonify({"answer": result.answer, "sources": result.sources})

    return app
