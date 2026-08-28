# groc

A grocery deal chat app, starting with a Phase 1 data pipeline: a scraper
that pulls flyer data from Flipp's public backend API and stores it in a
SQLite database.

See [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md) for the full project plan.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Usage

Scrape grocery flyers for one or more postal codes into a SQLite database:

```bash
python -m groc.cli scrape --postal-code M5V2H1 --db groc.db
```

Multiple postal codes:

```bash
python -m groc.cli scrape -p M5V2H1 -p M4B1B3 --db groc.db
```

By default only flyers categorized "Groceries" are captured (across *every*
grocery merchant Flipp returns for the postal code, not a hardcoded list of
stores). Use `--all-categories` to capture every flyer regardless of
category, or `--category` (repeatable) to pick specific categories.

```bash
python -m groc.cli scrape -p M5V2H1 --all-categories --db groc.db
```

Each run upserts rows keyed on
`(flyer_id, item_name, raw_price_text, postal_code, valid_from)`, so
re-running against the same flyer just refreshes `scraped_at` instead of
duplicating rows — data accumulates across runs instead of being overwritten.

## Searching

Search scraped items across every store, cheapest first:

```bash
python -m groc.cli search "chicken breast" --db groc.db
```

Restrict to one postal code, or collapse to the cheapest match per store:

```bash
python -m groc.cli search "chicken breast" --db groc.db -p M5V2H1 --best-per-merchant
```

Ranking prefers `unit_price` when a row has one (so differently-sized packages
stay comparable), falling back to plain `price` otherwise — in practice this
means plain price almost always, since real Flipp data rarely populates
`unit_price` (see Known limitations below).

## Asking questions (chat layer)

`ask` retrieves matching items from the database first, then passes those
results plus your question to Claude to generate a grounded conversational
answer — the model is instructed to answer only from the retrieved rows, not
to invent prices:

```bash
python -m groc.cli ask "what's the best deal on chicken breast" --db groc.db
```

Requires Claude API credentials to be available (`ANTHROPIC_API_KEY`, or an
`ant auth login` profile) — see the [Anthropic SDK docs](https://github.com/anthropics/anthropic-sdk-python)
for auth options.

## Web UI + API

A deliberately bare-bones (unstyled) web UI and JSON API sit on top of the
same search/chat layers — no design work has gone into this on purpose; it's
meant to be a functional base for a real design pass later, not a preview of one.

```bash
python -m groc.cli serve --db groc.db
```

Then open `http://127.0.0.1:5000/`. Endpoints:

- `GET /api/search?q=<query>&postal_code=<pc>&limit=<n>&best_per_merchant=1` — same ranking as the CLI `search` command
- `POST /api/ask` with JSON body `{"question": "...", "postal_code": "..."}` — returns `{"answer": "...", "sources": [...]}`, where `sources` are the raw flyer rows the answer was grounded in

For production-style serving (not the Flask dev server), a WSGI entrypoint
and Procfile are included:

```bash
pip install -e ".[prod]"
GROC_DB_PATH=/path/to/groc.db gunicorn groc.wsgi:app
```

Actually hosting this (choosing a provider, domain, deployment pipeline) is
still an open decision — see Phase 5 in `docs/PROJECT_PLAN.md`.

## Scheduling

Run the scraper daily with cron, e.g.:

```cron
0 6 * * * cd /path/to/groc && /path/to/.venv/bin/python -m groc.cli scrape -p M5V2H1 --db groc.db >> logs/scrape.log 2>&1
```

## Database schema

`flyer_items` table (SQLite, see `groc/db.py`):

| column | notes |
| --- | --- |
| `merchant` | store name, as returned by Flipp |
| `flyer_id` | Flipp's flyer id |
| `item_name` | |
| `raw_price_text` | the original price copy, e.g. `"was $6.99 now $4.99"` |
| `price` | parsed effective unit price |
| `was_price` | parsed pre-markdown price, if any |
| `unit_price` / `unit_label` | e.g. `1.99` / `"100g"` when per-unit pricing is present |
| `deal_quantity` | e.g. `2` for a "2 for $5" deal |
| `package_size` | e.g. `"1kg"`, parsed from price text or item name |
| `valid_from` / `valid_to` | flyer validity dates |
| `postal_code` | |
| `scraped_at` | UTC ISO timestamp of the last time this row was seen |

## Running tests

```bash
pytest
```

## Known limitations

- Real Flipp API data almost never populates `was_price`, `unit_price`, or
  `deal_quantity` — items only ever carry a plain current `price` (see
  `docs/PROJECT_PLAN.md` Phase 2 notes). Markdown/multi-buy detection in
  `price_parser.py` is exercised by unit tests but not by live data.
- Search/ask matching is literal keyword matching on `item_name` — no
  brand-variant normalization yet (e.g. "No Name" vs "PC" chicken breast
  aren't recognized as comparable products).
