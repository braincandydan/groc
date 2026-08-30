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

A mobile-first web UI (pine/marigold/cream, Archivo type, designed in Claude
Design) sits on top of the same search/chat layers, plus a JSON API.

```bash
python -m groc.cli serve --db groc.db
```

Then open `http://127.0.0.1:5000/`. It covers: onboarding (postal code entry),
browse/filter/sort with a filter sheet (sort, category, per-store checklist),
Ask (grounded Q&A with sources), a no-results state with suggestions, and a
**My List** saved-items feature (persisted in the browser's `localStorage`,
with a "which stores cover everything" suggestion).

API endpoints, if you're building your own frontend against this:

- `GET /api/search?q=<query>&postal_code=<pc>&limit=<n>&offset=<n>&best_per_merchant=1` — same ranking as the CLI `search` command; paginate with `limit`/`offset` and the response's `has_more` flag
- `POST /api/ask` with JSON body `{"question": "...", "postal_code": "..."}` — returns `{"answer": "...", "sources": [...]}`, where `sources` are the raw flyer rows the answer was grounded in

For production-style serving (not the Flask dev server), a WSGI entrypoint
and Procfile are included:

```bash
pip install -e ".[prod]"
GROC_DB_PATH=/path/to/groc.db gunicorn groc.wsgi:app
```

## Deploying on Vercel

Vercel runs the app as serverless functions with no persistent local disk, so
the SQLite file used everywhere above doesn't work there — `groc/db.py`
supports Postgres too for exactly this case. `db.connect()` picks the backend
automatically: pass it a `postgres://`/`postgresql://` URL and it uses
Postgres; anything else is treated as a SQLite file path. Every CLI command
(`scrape`, `search`, `ask`, `serve`) already works against either, unchanged
— just pass a Postgres URL as `--db`.

To deploy:

1. **Add a Postgres database** — in the Vercel dashboard, add Vercel Postgres
   (powered by Neon) or Neon directly from the Storage tab. Free tier is
   plenty for this. This auto-injects a `DATABASE_URL` (or `POSTGRES_URL`)
   env var into your deployment, which `groc/wsgi.py` picks up automatically.
2. **Connect the GitHub repo** to a new Vercel project — Vercel auto-detects
   the Flask app via `pyproject.toml`'s `[tool.vercel] entrypoint` and
   deploys it with no further config.
3. **Populate the database** — the scraper still needs to run somewhere with
   real Python execution and enough time to hit Flipp's API repeatedly
   (Vercel's serverless functions are the wrong shape for this). From your
   own machine or existing cron setup, point the same CLI at the hosted DB
   using the connection string from step 1:
   ```bash
   python -m groc.cli scrape -p M5V2H1 --db "postgresql://...(from Vercel/Neon)..."
   ```
   (`psycopg` is an unconditional dependency, installed by the normal `pip install -e ".[dev]"` from Setup above — no separate extra needed.)
   Re-run this on a schedule (e.g. your existing local cron) to keep data
   fresh; the deployed web app just reads whatever's in that database.

Local Postgres testing (without touching the real deployment) — the test
suite includes a set of backend-parity tests, skipped unless you point them
at a real Postgres instance:

```bash
docker run -d --name groc-test-pg -e POSTGRES_PASSWORD=groc \
    -e POSTGRES_DB=groc -p 5544:5432 postgres:16
GROC_TEST_POSTGRES_DSN=postgresql://postgres:groc@localhost:5544/groc pytest tests/test_postgres_backend.py
```

Non-Vercel hosting (Render/Railway/Fly.io, with normal persistent disk) still
works exactly as described above with a local SQLite file — no Postgres
needed for those.

## Scheduling

For a fixed list of postal codes on your own machine, cron works fine:

```cron
0 6 * * * cd /path/to/groc && /path/to/.venv/bin/python -m groc.cli scrape -p M5V2H1 --db groc.db >> logs/scrape.log 2>&1
```

**For the production deployment**, a GitHub Actions scheduled workflow
(`.github/workflows/scrape.yml`) re-scrapes every postal code anyone has
searched, once a day, so prices stay current as flyers update weekly and a
newly-searched postal code gets real data within a day instead of never.
This is what makes postal codes "dynamic" without needing instant on-demand
scraping (a full scrape takes 30-90+ seconds — too slow for a live web
request on serverless hosting, and there's no meaningful time limit on a
scheduled GitHub Actions job).

How it works: `/api/search` calls `db.track_postal_code()` on every request
with a postal code, recording it in the `tracked_postal_codes` table whether
it has data yet or not. The workflow runs:

```bash
python -m groc.cli scrape-tracked --db "$DATABASE_URL"
```

which re-scrapes every tracked postal code and updates `last_scraped_at`.

**One-time setup**: add the production Postgres connection string as a
repository secret named `DATABASE_URL` (Settings → Secrets and variables →
Actions → New repository secret), or via the CLI:

```bash
gh secret set DATABASE_URL --body "postgresql://...(from Vercel/Neon)..."
```

Trigger it manually to test before waiting for the schedule: Actions tab →
"Scheduled flyer scrape" → Run workflow.

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
| `cutout_image_url` | Flipp's cropped image of just this item's tag on the flyer page — the only place markdown/multi-buy pricing is actually visible (see Known limitations) |
| `category` | comma-joined categories of the flyer this item came from (e.g. `"Groceries"`, or `"All Flyers,Groceries"`) — a flyer-level tag, not per-item |

`cutout_image_url`/`category` were added after the initial schema; `init_db()` `ALTER TABLE`s them into any pre-existing database file automatically, so an older `groc.db` upgrades in place the next time it's scraped.

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
