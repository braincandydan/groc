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
