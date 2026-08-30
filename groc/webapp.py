"""HTTP API + frontend over the search/chat layers."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from . import db
from .chat import ask as chat_ask
from .scraper import DEFAULT_CATEGORIES, parse_and_store_flyer
from .search import best_by_merchant, search_items

_PLACES_PATH = Path(__file__).parent / "data" / "canadian_places.json"
_FSA_CENTROIDS_PATH = Path(__file__).parent / "data" / "fsa_centroids.json"
_json_file_cache: dict = {}

# Bounds on the client-submitted ingest payload -- generous enough for any
# real postal code while keeping a single request's DB work bounded
# regardless of what a client sends. Verified against real Flipp data for a
# dense Toronto FSA (M5V2H1): 63 grocery-category flyers, the largest single
# flyer (Walmart) had 933 items -- MAX_INGEST_ITEMS_PER_FLYER was originally
# set to 500 from a guess and rejected that entire real submission; a flyer
# over the cap is now truncated (see api_ingest_scrape), not rejected
# outright, as a second line of defense against an even larger real flyer.
MAX_INGEST_FLYERS = 200
MAX_INGEST_ITEMS_PER_FLYER = 2000

_POSTAL_CODE_RE = re.compile(r"^[A-Z]\d[A-Z]\d[A-Z]\d$")


def _load_cached_json(path: Path) -> list:
    # Cached at module scope keyed by path: these files never change at
    # runtime, and a cold Vercel function would otherwise re-read+re-parse
    # them on every single request.
    if path not in _json_file_cache:
        with open(path, encoding="utf-8") as f:
            _json_file_cache[path] = json.load(f)
    return _json_file_cache[path]


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
    # /api/ingest-scrape accepts client-submitted JSON (a browser's own raw
    # Flipp fetch); this bounds the whole request body regardless of the
    # per-list caps enforced in the handler itself.
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024

    def _connect() -> sqlite3.Connection:
        # init_db() every connection (not just from the CLI's scrape commands)
        # so a schema change (e.g. a new table) doesn't 500 in production
        # until someone remembers to run a migration by hand -- exactly what
        # happened with tracked_postal_codes. Cheap: idempotent DDL only.
        conn = db.connect(app.config["DB_PATH"])
        db.init_db(conn)
        return conn

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/favicon.ico")
    def favicon():
        return "", 204

    @app.get("/api/places")
    def api_places():
        # Every Canadian city/town from GeoNames' open postal code dataset
        # (CC BY 4.0), one real postal code per place -- lets the "choose
        # your city" picker cover the whole country, not just a curated
        # shortlist. Static per deploy, so cache client-side for a day.
        resp = jsonify(_load_cached_json(_PLACES_PATH))
        resp.cache_control.public = True
        resp.cache_control.max_age = 86400
        return resp

    @app.get("/api/fsa-centroids")
    def api_fsa_centroids():
        # One real lat/long centroid + real postal code per Canadian FSA
        # (first 3 characters of a postal code), derived from the same
        # GeoNames dataset as /api/places -- lets "Use my location" find the
        # nearest FSA to a raw GPS coordinate entirely client-side, no
        # external geocoding API/cost. [fsa, postal_code, lat, lon].
        resp = jsonify(_load_cached_json(_FSA_CENTROIDS_PATH))
        resp.cache_control.public = True
        resp.cache_control.max_age = 86400
        return resp

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
        postal_code_scraped = False
        ingest_token = None
        if postal_code:
            # Lets the scheduled scraper (groc scrape-tracked) pick up a
            # postal code the next time it runs, even if it has zero data
            # right now -- see docs/PROJECT_PLAN.md Phase 5. A brand-new
            # postal code with zero results gets scraped client-side instead
            # of waiting on that schedule (see /api/ingest-scrape) -- this
            # flag tells the frontend whether that's already happened, so it
            # can tell "haven't checked yet" (trigger a client-side scrape)
            # apart from "checked, genuinely nothing here" (don't retry).
            db.track_postal_code(conn, postal_code)
            postal_code_scraped = db.get_postal_code_scraped_at(conn, postal_code) is not None
            if not postal_code_scraped:
                # Only issued when a client-side scrape is actually the next
                # step -- required by /api/ingest-scrape so a submission must
                # be preceded by a real search for this exact postal code,
                # not fabricated out of nowhere by an unrelated caller.
                ingest_token = db.issue_ingest_token(conn, postal_code)
        rows = search_items(conn, query, postal_code=postal_code, limit=limit, offset=offset)
        # Whether the client might need another page -- best_per_merchant
        # collapses rows *after* this check, since it answers "was this page
        # of the underlying data full", not "did filtering leave more to show".
        has_more = len(rows) == limit
        if best_per_merchant:
            rows = best_by_merchant(rows)
        return jsonify({
            "results": [_row_to_dict(r) for r in rows],
            "has_more": has_more,
            "postal_code_scraped": postal_code_scraped,
            "ingest_token": ingest_token,
        })

    @app.post("/api/ingest-scrape")
    def api_ingest_scrape():
        """Store a client-side scrape's raw Flipp payload.

        The browser fetches Flipp's flyer/item endpoints directly (CORS is
        open there) and POSTs the RAW, unparsed JSON here -- parsing/
        validating prices and names happens only in parse_and_store_flyer,
        the same trusted path the server-side scraper uses. A client must
        never be able to pre-parse its own rows: that would mean trusting
        arbitrary browser-submitted prices/merchants with no real
        verification against Flipp's actual data.

        Requires a token from a prior /api/search response for this exact
        postal code (see db.issue_ingest_token/redeem_ingest_token) --
        without this, shape validation alone would still let any caller
        submit fabricated flyer data for any postal code with no connection
        to an actual scrape /api/search ever kicked off.

        Every input is untrusted: shape/size is validated defensively so a
        malformed or oversized payload gets a clean 400, never a 500 (a
        single bad flyer/item within an otherwise-valid payload is skipped,
        not fatal -- see parse_and_store_flyer).
        """
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "invalid JSON body"}), 400

        postal_code = _normalize_postal_code(payload.get("postal_code"))
        if not postal_code or not _POSTAL_CODE_RE.match(postal_code):
            return jsonify({"error": "invalid or missing 'postal_code'"}), 400

        token = payload.get("token")
        if not isinstance(token, str) or not token:
            return jsonify({"error": "missing required field 'token'"}), 401

        conn = _connect()
        if not db.redeem_ingest_token(conn, token, postal_code):
            return jsonify({"error": "invalid, expired, or already-used token"}), 401

        flyers_payload = payload.get("flyers")
        if not isinstance(flyers_payload, list):
            return jsonify({"error": "'flyers' must be a list"}), 400
        if len(flyers_payload) > MAX_INGEST_FLYERS:
            return jsonify({"error": f"too many flyers (max {MAX_INGEST_FLYERS})"}), 400

        for entry in flyers_payload:
            if not isinstance(entry, dict):
                return jsonify({"error": "each entry in 'flyers' must be an object"}), 400
            if not isinstance(entry.get("flyer"), dict):
                return jsonify({"error": "each entry needs a 'flyer' object"}), 400
            if not isinstance(entry.get("items"), list):
                return jsonify({"error": "each entry needs an 'items' list"}), 400

        total_stored = 0
        for entry in flyers_payload:
            # Truncate rather than reject the whole request over one large
            # flyer -- confirmed against real Flipp data that a single
            # big-box flyer can genuinely have 900+ items (Walmart, a real
            # M5V2H1 flyer, had 933), so rejecting the entire ~60-flyer
            # submission over one legitimately large flyer would silently
            # discard everything else that was actually fine.
            items = entry["items"][:MAX_INGEST_ITEMS_PER_FLYER]
            total_stored += parse_and_store_flyer(
                conn, entry["flyer"], items, postal_code, categories=DEFAULT_CATEGORIES,
            )

        db.track_postal_code(conn, postal_code)
        db.mark_postal_code_scraped(conn, postal_code, db.utcnow_iso())
        return jsonify({"stored": total_stored})

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
