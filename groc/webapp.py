"""HTTP API + frontend over the search/chat layers."""
from __future__ import annotations

import sqlite3
from typing import Optional

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

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

    @app.get("/favicon.ico")
    def favicon():
        return "", 204

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
            offset = int(request.args.get("offset", 0))
        except ValueError:
            return jsonify({"error": "'limit' and 'offset' must be integers"}), 400

        conn = _connect()
        rows = search_items(conn, query, postal_code=postal_code, limit=limit, offset=offset)
        # Whether the client might need another page -- best_per_merchant
        # collapses rows *after* this check, since it answers "was this page
        # of the underlying data full", not "did filtering leave more to show".
        has_more = len(rows) == limit
        if best_per_merchant:
            rows = best_by_merchant(rows)
        return jsonify({"results": [_row_to_dict(r) for r in rows], "has_more": has_more})

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

    @app.errorhandler(Exception)
    def handle_error(e):
        # HTTPException (404s, etc.) already has its own correct response --
        # let Flask serve it normally instead of treating routine routing as
        # a crash (this used to make every /favicon.ico request log as a 500).
        if isinstance(e, HTTPException):
            return e

        # Flask's default error page is HTML, which breaks the frontend's
        # `.json()` parsing with a cryptic "Unexpected token '<'" instead of
        # showing what actually went wrong. Log the real exception server-side
        # (visible in Vercel's function logs) but never echo it to the client
        # -- a DB connection failure's message can include the DSN/credentials.
        app.logger.exception("unhandled error handling %s %s", request.method, request.path)
        if request.path.startswith("/api/"):
            return jsonify({"error": "internal server error"}), 500
        raise e

    return app
