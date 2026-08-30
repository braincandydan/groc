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
- [x] Simple chat interface (web) — `groc/webapp.py` (Flask) + `groc/templates/index.html`, run via `groc serve`.
- [x] Postal code / region setting — a postal code field on the page, threaded through to both `/api/search` and `/api/ask`. Preferred store selection (the "optionally" part) not implemented as its own control, but the filter sheet's per-store checklist covers the same need.
- [x] Basic UI to show source items behind an answer — `/api/ask` now returns `sources` (the raw rows `chat.ask()` was grounded in, via the new `AskResult` return type) and the Ask sheet renders them under the answer.
- [x] Postal code loads the full item list once, then a second field filters it client-side as you type (no per-keystroke server round-trip) — reworked from the original per-query-only search after trying it live.
- [x] Two new DB columns added on request: `cutout_image_url` (the item's cropped flyer-page image — the only place markdown/multi-buy pricing is actually visible) and `category` (the flyer's own category tag). Both used in the redesigned UI.
- [x] Real visual design — designed in Claude Design (project "Groc grocery deal comparison", file `groc 2a - screens.dc.html`, pulled in via `/design-login` + `DesignSync`) and implemented into `templates/index.html`: a pine/marigold/cream palette, Archivo type, sharp-edged (no border-radius except pill toggles) mobile-first layout. Covers onboarding, the paged load, browse/filter/sort, a filter sheet (sort by price/store/expiring, category facets, per-store checklist), Ask (empty/thinking/answered), no-results with suggestions, and a new **My List** feature (add/remove, running total, "which stores cover everything" via a greedy set-cover, persisted in `localStorage`).
  - Two spots adapted from the mockup to match real data: dropped the mockup's per-store *distance* (no geolocation/store-coordinate data exists to compute it) and used the real flyer-level `category` values (e.g. "Groceries", "Pharmacy") as the category facet instead of inventing a produce/dairy/meat taxonomy that isn't in the actual data.
  - Verified with a real headless-browser pass (Playwright), not just read — confirmed the full flow end-to-end and caught one real bug (the header's status/toggle row stayed visible on the My List tab) before considering it done.
  - Also fixed in passing: `/favicon.ico` and other routine 404s were being logged and served as 500s, because the API's catch-all error handler was intercepting `HTTPException` too. Now passes those through untouched.

## Phase 5 — Hosting/infra
- [x] Provider chosen: Vercel (free Hobby tier) for the web app, since serverless functions have no persistent local disk, `groc/db.py` now supports Postgres as a second backend alongside SQLite — `connect()` picks the backend from the DSN, so every existing CLI command works against either unchanged, no new commands/flags needed.
  - Verified against a real local Postgres container (not just assumed to work): found and fixed one real cross-backend bug in the process — SQLite's `LIKE` is case-insensitive by default, Postgres's isn't, so `search_items()` silently returned zero rows for any query on Postgres until fixed to explicitly `LOWER()` both sides. `tests/test_postgres_backend.py` (10 tests, skipped unless `GROC_TEST_POSTGRES_DSN` is set) locks this in as a regression test.
  - `pyproject.toml`'s `[tool.vercel] entrypoint = "groc.wsgi:app"` + `vercel.json` (bundle-size excludes) are the actual deploy config — Vercel auto-detects the Flask app from there, no `api/` directory needed with the current Vercel Python runtime.
- [x] Deployed and live at https://groc-2.vercel.app, with a real Postgres (Neon) database attached. `M5V2H1` and `V1Y7M4` have real scraped data.
- [x] Host for the scheduled scraper — a GitHub Actions scheduled workflow (`.github/workflows/scrape.yml`, daily), running the existing scraper unchanged against the production Postgres URL. Chosen because it's free, has no meaningful execution-time limit (unlike Vercel's request-scoped functions, which can't run a 30-90s scrape), and needs no extra hosting.
- [x] Postal codes are dynamic, with a delay rather than instantly: `/api/search` now calls `db.track_postal_code()` on every request with a postal code (new table `tracked_postal_codes`), and the scheduled workflow re-scrapes every tracked postal code (`groc scrape-tracked`) whether it already has data (keeps prices fresh as flyers update weekly) or has none yet (a postal code searched for the first time gets scraped on the next scheduled run). True on-demand/instant scraping isn't feasible here — a full scrape takes 30-90+ seconds, far past what a single web request can wait for on serverless hosting.
  - Still needs the user to add the Postgres connection string as a GitHub Actions secret (`DATABASE_URL`) for the workflow to actually run — an account-level step.
- [x] ~~Cut the "up to 24 hours" wait down further via an immediate GitHub Actions dispatch (`groc/github_trigger.py`)~~ — **removed and replaced with client-side scraping** (below). The immediate-trigger approach had a real scaling bug: it re-ran the same "scrape every tracked postal code" job (`scrape-tracked`) rather than just the new one, so its actual latency grew with every postal code ever tracked instead of staying fast — measured real GitHub Actions runs ranged 30s–2m35s and would only get slower over time.
- [x] **Client-side scraping** for a brand-new postal code, replacing the above. Flipp's API sets `Access-Control-Allow-Origin: *` (verified live) so a user's own browser fetches Flipp's flyer/item endpoints directly — no server queue, no GitHub Actions dependency, latency is just however long the real fetch takes (~40s for a dense Toronto FSA with 63 grocery flyers/8,440 items in a real end-to-end test). The browser only ever fetches and forwards **raw, unparsed** Flipp JSON to a new `/api/ingest-scrape` endpoint; parsing/validating prices and merchant names happens exactly once, server-side, via `groc/scraper.py`'s `parse_and_store_flyer` — the same trusted path the server-side scraper already used (refactored out of `scrape_postal_code` so both share it). A client can never fabricate prices/data by pre-parsing its own rows.
  - `/api/search` now reports `postal_code_scraped` (backed by `db.get_postal_code_scraped_at()`) so the frontend can tell "haven't successfully checked this postal code yet" (triggers a client-side scrape) apart from "checked, genuinely nothing here" (shows a calmer empty state, doesn't retry every visit). A failed scrape attempt (network error, Flipp timeout) deliberately does *not* mark the postal code as scraped, so it naturally retries on a later visit instead of being mistaken for a confirmed-empty area.
  - New attack surface (`/api/ingest-scrape` accepts client-submitted JSON) hardened defensively: postal code format validation, payload size caps (`MAX_INGEST_FLYERS`, `MAX_INGEST_ITEMS_PER_FLYER`, an 8MB `MAX_CONTENT_LENGTH`), malformed individual flyers/items are skipped rather than failing the whole batch, and `_flyer_categories`/`_build_row` were hardened against garbage input types (a client-submitted `categories: 5` or non-string `cutout_image_url` no longer crashes into a 500). Real Flipp data caught a sizing bug in this hardening before it shipped: a real Walmart flyer for M5V2H1 had 933 items, over an initial 500-item guessed cap — fixed by raising the cap to 2000 *and* truncating an over-cap flyer instead of rejecting the entire ~60-flyer submission over one large flyer.
  - Verified: 106 unit/integration tests (120 with Postgres) covering the new endpoint's validation/idempotency/parity with the server-side path; Playwright with Flipp route-interception for the happy path and both failure modes (fetch failure, genuinely-empty area); and one real, unmocked end-to-end run against the actual Flipp API for M5V2H1 confirming real data flows all the way through.
- [x] City picker as an easier alternative to typing an exact postal code — "CHOOSE YOUR CITY" is now the primary onboarding action (postal code entry is a secondary "Or enter your postal code" link), and "Choose city instead" in the header does the same after onboarding. Originally shipped with a curated 115-city shortlist; upgraded after real user feedback ("people need to be able to put any city or town in Canada") to cover all 7,324 unique places in GeoNames' open Canadian postal code dataset (CC BY 4.0), served from a new `/api/places` endpoint (`groc/data/canadian_places.json`) and fetched once client-side rather than embedded inline, so page weight stays small. Every place still maps to one real, never-fabricated postal code. Picking a city just fills in that postal code and submits the existing flow, so search/tracking/on-demand scraping all work unchanged. Verified with Playwright (both entry points, live filtering including small towns outside the old 115, result capping on broad queries, selection, Escape-to-close, no mobile overflow/zoom regressions).
  - "Use my location" (browser geolocation → reverse-geocode to a postal code) is a deliberately separate, not-yet-built decision — it needs a provider choice (Google Maps Geocoding costs money past its free quota; Nominatim/OSM is free but rate-limited with patchier Canadian precision) that should go back to the user before building.
- [x] My List UX pass, per user request: check off items while shopping (strikethrough without removing, so the list survives the trip as a reference), swipe-to-remove via pointer events (works for touch and mouse; the REMOVE button stays too for keyboard/screen-reader access), an undo toast on every removal, and "Done shopping — archive this list" which clears the active list into a localStorage archive (last 20 trips) instead of deleting it outright. Caught and fixed two real bugs along the way: My List was pushing the same row object reference as the Deals-list search result (so check-off would've struck through the item in both tabs), and the swipe-remove backdrop bled through on top of a checked-off row due to opacity making the row's "opaque" background translucent. Verified with Playwright (22 checks) plus before/after screenshots.

## Known open questions / risks
- Terms-of-service: this hits Flipp's API directly rather than their public site pages; keep scope/personal-use in mind as it grows
- Flyer data is messy — expect ongoing cleanup work as formats vary by store
- Item matching across stores is the hardest technical piece and will likely need iteration

## Suggested build order
1. Get the scraper working end-to-end with a real database (not CSV)
2. Get basic retrieval/search working against that database
3. Wire up the chat layer on top
4. Build the frontend last, once the data + chat logic actually work
