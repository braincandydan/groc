"""Command-line entry point: `python -m groc.cli scrape --postal-code A1A1A1`."""
from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional, Sequence

from . import db
from .chat import ask as chat_ask
from .scraper import DEFAULT_CATEGORIES, run, run_tracked
from .search import best_by_merchant, search_items


def _valid_postal_code(value: str) -> str:
    cleaned = value.strip().upper().replace(" ", "")
    if len(cleaned) != 6:
        raise argparse.ArgumentTypeError(f"invalid postal code: {value!r}")
    return cleaned


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="groc", description="Scrape grocery flyer deals from Flipp")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scrape = subparsers.add_parser("scrape", help="Fetch flyer data and store it in the database")
    scrape.add_argument(
        "--postal-code", "-p", dest="postal_codes", action="append", required=True,
        type=_valid_postal_code, help="Postal code to scrape (repeat for multiple)",
    )
    scrape.add_argument("--db", default="groc.db", help="Path to the SQLite database file")
    scrape.add_argument(
        "--all-categories", action="store_true",
        help="Do not filter flyers by category (default: only 'Groceries')",
    )
    scrape.add_argument(
        "--category", dest="categories", action="append",
        help="Flyer category to include (repeat for multiple); default: Groceries",
    )
    scrape.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")

    search = subparsers.add_parser("search", help="Search scraped flyer items across stores")
    search.add_argument("query", help="Search terms, e.g. 'chicken breast'")
    search.add_argument("--db", default="groc.db", help="Path to the SQLite database file")
    search.add_argument("--postal-code", "-p", type=_valid_postal_code, help="Restrict to one postal code")
    search.add_argument("--limit", type=int, default=20, help="Max rows to show (default: 20)")
    search.add_argument(
        "--best-per-merchant", action="store_true",
        help="Show only the cheapest matching item per merchant",
    )
    search.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")

    ask = subparsers.add_parser("ask", help="Ask a grocery-deal question, answered by Claude grounded in scraped data")
    ask.add_argument("question", help="Question to ask, e.g. 'what's the best deal on chicken breast'")
    ask.add_argument("--db", default="groc.db", help="Path to the SQLite database file")
    ask.add_argument("--postal-code", "-p", type=_valid_postal_code, help="Restrict to one postal code")
    ask.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")

    scrape_tracked = subparsers.add_parser(
        "scrape-tracked",
        help="Re-scrape every postal code anyone has searched, for a scheduled job (e.g. GitHub Actions)",
    )
    scrape_tracked.add_argument("--db", default="groc.db", help="Path to the SQLite database file or a Postgres URL")
    scrape_tracked.add_argument(
        "--all-categories", action="store_true",
        help="Do not filter flyers by category (default: only 'Groceries')",
    )
    scrape_tracked.add_argument(
        "--category", dest="categories", action="append",
        help="Flyer category to include (repeat for multiple); default: Groceries",
    )
    scrape_tracked.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")

    serve = subparsers.add_parser("serve", help="Run the web UI + API (search and ask)")
    serve.add_argument("--db", default="groc.db", help="Path to the SQLite database file")
    serve.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    serve.add_argument("--port", type=int, default=5000, help="Port to bind (default: 5000)")
    serve.add_argument("--debug", action="store_true", help="Run Flask in debug/auto-reload mode")
    serve.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command == "scrape":
        categories = None if args.all_categories else set(args.categories or DEFAULT_CATEGORIES)
        total = run(args.postal_codes, args.db, categories=categories)
        print(f"Stored/updated {total} flyer item rows in {args.db}")
        return 0

    if args.command == "search":
        conn = db.connect(args.db)
        rows = search_items(conn, args.query, postal_code=args.postal_code, limit=args.limit)
        if args.best_per_merchant:
            rows = best_by_merchant(rows)
        _print_results(rows)
        return 0

    if args.command == "scrape-tracked":
        categories = None if args.all_categories else set(args.categories or DEFAULT_CATEGORIES)
        results = run_tracked(args.db, categories=categories)
        if not results:
            print("No tracked postal codes yet -- nothing to do.")
        for postal_code, total in results.items():
            print(f"{postal_code}: stored/updated {total} rows")
        print(f"Done. {len(results)} postal code(s) refreshed.")
        return 0

    if args.command == "ask":
        conn = db.connect(args.db)
        result = chat_ask(conn, args.question, postal_code=args.postal_code)
        print(result.answer)
        return 0

    if args.command == "serve":
        from .webapp import create_app

        app = create_app(db_path=args.db)
        app.run(host=args.host, port=args.port, debug=args.debug)
        return 0

    parser.error("unknown command")
    return 2


def _print_results(rows) -> None:
    if not rows:
        print("No matches.")
        return
    for row in rows:
        effective = row["unit_price"] if row["unit_price"] is not None else row["price"]
        unit = f"/{row['unit_label']}" if row["unit_label"] else ""
        price_str = f"${effective:.2f}{unit}" if effective is not None else "n/a"
        deal = f" ({row['deal_quantity']} for ...)" if row["deal_quantity"] else ""
        print(f"{row['merchant']:<28} {row['item_name']:<45} {price_str}{deal}  (valid to {row['valid_to']})")


if __name__ == "__main__":
    sys.exit(main())
