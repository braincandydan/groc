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
- [x] Query the database for matches — `groc/search.py` + `groc search "<query>"` CLI (tokenized keyword match on item_name). Not yet exposed as an HTTP API; that's still open when Phase 4 needs one.
- [x] Cross-store comparison logic — same query returns matches across every store, ranked cheapest-first (`--best-per-merchant` collapses to one row per store)
- [ ] Item-matching/normalization (harder problem) — e.g. recognizing "No Name Chicken Breast 1kg" and "PC Chicken Breast Value Pack" refer to comparable products. Start simple (keyword/fuzzy match) and improve later
- [ ] Unit price normalization so different pack sizes are actually comparable — blocked in practice: real Flipp data never populates `unit_price` (see smoke-test findings), so ranking currently falls back to plain price for effectively all items. Package size is still parsed from item names (`package_size` column) but isn't yet used to normalize price-per-unit across different pack sizes.

## Phase 3 — Chat layer
- [x] Connect to an LLM via API — `groc/chat.py` uses the Claude API (`claude-opus-5`), `groc ask "<question>"` CLI
- [x] On each user question: retrieve matching items from the database first (via Phase 2's `search_items`), then pass those results + the question to the model to generate a conversational answer
- [x] Prompt design to keep answers grounded in actual retrieved data, not guessed prices — system prompt instructs the model to answer only from the retrieved list and say so plainly when nothing matches
- [ ] Support richer conversational asks like "what should I buy this week to save money" or "suggest a meal using stuff that's on sale" — current keyword extraction is a simple stopword strip, tuned for direct item lookups ("best deal on X"); broader/aggregate questions aren't retrieval-shaped yet and would need different query logic
- [ ] Not yet tested against the live Claude API — deliberately skipped in the autonomous build loop to avoid spending API credits without explicit sign-off; wiring is unit-tested with a fake client instead. Try `groc ask "..."` yourself once you're ready to spend a live call on it.

## Phase 4 — Frontend
- [ ] Simple chat interface (web to start; mobile later if useful)
- [ ] Postal code / region setting, and optionally preferred store selection
- [ ] Basic UI to show source items behind an answer (store, price, valid dates)

## Phase 5 — Hosting/infra
- [ ] Host for the scheduled scraper + database
- [ ] Host for the chat backend/frontend

## Known open questions / risks
- Terms-of-service: this hits Flipp's API directly rather than their public site pages; keep scope/personal-use in mind as it grows
- Flyer data is messy — expect ongoing cleanup work as formats vary by store
- Item matching across stores is the hardest technical piece and will likely need iteration

## Suggested build order
1. Get the scraper working end-to-end with a real database (not CSV)
2. Get basic retrieval/search working against that database
3. Wire up the chat layer on top
4. Build the frontend last, once the data + chat logic actually work
