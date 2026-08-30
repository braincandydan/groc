# Grocery Deal Chat App — Project Plan

## Overview
An AI chat app that helps users find grocery deals, compare prices across stores, and get suggestions on what to buy based on current flyer data. Built on top of Flipp's backend API (the same one their app/site uses), which returns structured flyer data directly — no image OCR needed.

## Reference implementation
Starting point: https://github.com/Kiizon/flippscrape

Confirmed working API endpoints (no key required, just a random session id):
```
FLYERS       = https://flyers-ng.flippback.com/api/flipp/data?locale=en&postal_code={postal_code}&sid={sid}
FLYER_ITEMS  = https://flyers-ng.flippback.com/api/flipp/flyers/{flyer_id}/flyer_items?locale=en&sid={sid}
```
- `sid` = random 16-digit string, doesn't need to be a "real" session
- `FLYERS` returns all flyers active for a postal code (all merchants)
- `FLYER_ITEMS` returns every item in one flyer (name, price, valid dates)

## Phase 1 — Data pipeline
- [x] Fork/rewrite the reference scraper in Python
- [x] Remove the hardcoded 4-store limit — capture all merchants returned, not just No Frills/FreshCo/Walmart/Loblaws
- [x] Add price cleanup/parsing (handle "was $X now $Y", multi-buy deals like "2 for $5", per-unit pricing where available)
- [x] Replace CSV output with a real database (Postgres or SQLite to start) so data accumulates over time instead of being overwritten each run
- [x] Schema (starting point):
  - `merchant`, `flyer_id`, `item_name`, `raw_price_text`, `price`, `unit_price`, `package_size`, `valid_from`, `valid_to`, `postal_code`, `scraped_at`
- [ ] Schedule the scraper to run daily (cron job or hosted scheduler) for one or more postal codes — see README for a cron example; actually scheduling it is an infra step for deployment

## Phase 2 — Backend / retrieval
- [x] Query the database for matches — `groc/search.py` + `groc search "<query>"` CLI (tokenized keyword match on item_name), plus `GET /api/search` in Phase 4's web app
- [x] Cross-store comparison logic — same query returns matches across every store, ranked cheapest-first (`--best-per-merchant` collapses to one row per store)
- [ ] Item-matching/normalization (harder problem) — e.g. recognizing "No Name Chicken Breast 1kg" and "PC Chicken Breast Value Pack" refer to comparable products. Start simple (keyword/fuzzy match) and improve later
- [ ] Unit price normalization so different pack sizes are actually comparable — blocked in practice: real Flipp data never populates `unit_price` (see smoke-test findings), so ranking currently falls back to plain price for effectively all items. Package size is still parsed from item names (`package_size` column) but isn't yet used to normalize price-per-unit across different pack sizes.

## Phase 3 — Chat layer
- [x] Connect to an LLM via API — `groc/chat.py` uses the Claude API (`claude-opus-5`), `groc ask "<question>"` CLI
- [x] On each user question: retrieve matching items from the database first (via Phase 2's `search_items`), then pass those results + the question to the model to generate a conversational answer
- [x] Prompt design to keep answers grounded in actual retrieved data, not guessed prices — system prompt instructs the model to answer only from the retrieved list and say so plainly when nothing matches
- [x] Support richer conversational asks like "what should I buy this week to save money" — when stopword-stripping leaves no product-like keywords, `ask()` falls back to `search.top_deals()` (overall cheapest items, no item-name filter) instead of keyword-matching nonsense like "save money" against item names. Item-specific questions still take priority even when filler words like "suggest"/"to save money" are also present.
  - Known noise: `top_deals()` can surface non-grocery items (e.g. a candle tin, a report cover) because Flipp categorizes whole flyers, not individual items — a big-box store's single "Groceries"-tagged flyer still contains its other departments. `price > 0` filtering removed the worst offenders (Walmart's $0 subsidized-phone rows), but a proper fix would need a food-keyword allowlist/classifier, not attempted yet.
- [ ] "Suggest a meal using stuff that's on sale" specifically — needs multiple complementary items reasoned about together (an actual recipe), which `top_deals()`'s flat cheapest-items list doesn't really support well yet; still open.
- [ ] Not yet tested against the live Claude API — deliberately skipped in the autonomous build loop to avoid spending API credits without explicit sign-off; wiring is unit-tested with a fake client instead. Try `groc ask "..."` yourself once you're ready to spend a live call on it.

## Phase 4 — Frontend
- [x] Simple chat interface (web) — `groc/webapp.py` (Flask) + `groc/templates/index.html`, run via `groc serve`. Deliberately unstyled/bare-bones on purpose — no design investment yet, the user wants to do that pass themselves on top of working functionality. Mobile not started.
- [x] Postal code / region setting — a postal code field on the page, threaded through to both `/api/search` and `/api/ask`. Preferred store selection (the "optionally" part) not implemented.
- [x] Basic UI to show source items behind an answer — `/api/ask` now returns `sources` (the raw rows `chat.ask()` was grounded in, via the new `AskResult` return type) and the page renders them in a table under the answer.
- [x] Postal code loads the full item list once, then a second field filters it client-side as you type (no per-keystroke server round-trip) — reworked from the original per-query-only search after trying it live.
- [x] Two new DB columns added on request: `cutout_image_url` (the item's cropped flyer-page image — the only place markdown/multi-buy pricing is actually visible) and `category` (the flyer's own category tag). Both shown in the results table. `init_db()` migrates existing DB files in place.

## Phase 5 — Hosting/infra
- [x] Provider chosen: Vercel (free Hobby tier) for the web app, since serverless functions have no persistent local disk, `groc/db.py` now supports Postgres as a second backend alongside SQLite — `connect()` picks the backend from the DSN, so every existing CLI command works against either unchanged, no new commands/flags needed.
  - Verified against a real local Postgres container (not just assumed to work): found and fixed one real cross-backend bug in the process — SQLite's `LIKE` is case-insensitive by default, Postgres's isn't, so `search_items()` silently returned zero rows for any query on Postgres until fixed to explicitly `LOWER()` both sides. `tests/test_postgres_backend.py` (10 tests, skipped unless `GROC_TEST_POSTGRES_DSN` is set) locks this in as a regression test.
  - `pyproject.toml`'s `[tool.vercel] entrypoint = "groc.wsgi:app"` + `vercel.json` (bundle-size excludes) are the actual deploy config — Vercel auto-detects the Flask app from there, no `api/` directory needed with the current Vercel Python runtime.
- [ ] Not yet actually deployed — needs the user to connect the GitHub repo in the Vercel dashboard and add a Postgres database (Vercel Postgres/Neon, from the Storage tab), both account-level actions that can't be done from here.
- [~] Host for the scheduled scraper — still an open decision. The scraper can point at the same hosted Postgres URL via the existing `scrape` command (`--db "postgresql://..."`), but *where* that command runs on a schedule against production (the user's own machine's cron, a small VM, GitHub Actions, etc.) isn't decided yet.

## Known open questions / risks
- Terms-of-service: this hits Flipp's API directly rather than their public site pages; keep scope/personal-use in mind as it grows
- Flyer data is messy — expect ongoing cleanup work as formats vary by store
- Item matching across stores is the hardest technical piece and will likely need iteration

## Suggested build order
1. Get the scraper working end-to-end with a real database (not CSV)
2. Get basic retrieval/search working against that database
3. Wire up the chat layer on top
4. Build the frontend last, once the data + chat logic actually work
