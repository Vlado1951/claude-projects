# Implementation Plan — v2 Free Signal Sources

This plan picks up at **Phase 11**, after v1 is shipped. Each phase is sized to fit in a single Claude Code session and builds on top of `SPEC_V2.md`.

## How to use this plan with Claude Code

1. `cd` into your existing project (Phases 0–10 already on `main`).
2. Make sure `SPEC_V2.md` and `PLAN_V2.md` are at the project root next to the v1 docs.
3. Run `claude` and start with: *"Read `SPEC_V2.md` and `PLAN_V2.md`. v1 is complete on `main`. We're starting Phase 11."*
4. After each phase: run acceptance checks, commit, then proceed.

Phases follow the same shape as v1 — explicit acceptance criteria, no phase begins until the previous one is committed.

---

## Phase 11 — Refactor: formalize NewsSource Protocol

**Goal:** prepare the codebase for multiple listeners. No new functionality.

**Tasks:**
- Define `oilbot/news/source.py` with the `NewsSource` Protocol per SPEC_V2 §4.1.
- Update `TruthSocialListener` to set `name = "truth_social"` and conform.
- Add `source: str` column to the `seen_posts` table; one-time migration on startup if missing; backfill existing rows with `'truth_social'`.
- Update the orchestrator to accept a *list* of `NewsSource` instances instead of a single listener. Loop over them, multiplex into one queue.
- Add `NEWS_SOURCES` to config (`pydantic-settings`), default `"truth_social"`. Orchestrator filters its constructed listener list by this allow-list.
- Tests: existing tests still pass; add one that the orchestrator handles 0 enabled sources gracefully (logs a warning, doesn't crash).

**Acceptance:**
- `uv run pytest` — all green.
- `make lint test` clean.
- `python -m oilbot run` behaves identically to v1.
- `data/bot.db` schema shows `seen_posts.source` populated with `'truth_social'`.

---

## Phase 12 — Generic RSS listener

**Goal:** one reusable listener for all RSS-based sources.

**Tasks:**
- Implement `oilbot/news/rss.py` with `GenericRSSListener` per SPEC_V2 §4.2.
- Use `feedparser` (pure-Python, no system deps).
- Per-source dedupe via the `seen_posts` table with the new `source` column.
- Optional `keyword_filter` evaluated case-insensitively over the entry's title + summary.
- Optional `default_sentiment` annotated on emitted events as a hint that bypasses the classifier (used by NOAA).
- Optional `active_window` callable; when it returns False, the listener sleeps the poll interval without fetching.
- Backoff on HTTP errors: exponential, capped at 5 min, same as Truth Social listener.
- Add a `test-rss <source-name>` CLI subcommand: runs one poll and prints what would be emitted.
- Unit tests with a mock `feedparser` that returns scripted entries.

**Acceptance:**
- `uv run pytest` — all green.
- `python -m oilbot test-rss --feed-url <any RSS URL> --name dev` prints recent items.
- The new listener integrates with the orchestrator (you can wire one in temporarily for the test, even before adding the real sources).

---

## Phase 13 — OPEC + Reuters + NOAA listeners

**Goal:** plug three real RSS sources into the bot.

**Tasks:**
- Add a config map `oilbot/news/configured_sources.py` with the three sources defined per SPEC_V2 §3.3 / §3.4 / §3.5: feed URL, poll interval, keyword filter, default sentiment, active window where applicable.
- Hurricane season check: `active_window = lambda now: now.month in (6, 7, 8, 9, 10, 11)`.
- Update orchestrator to construct each enabled source as a `GenericRSSListener` from this map.
- Add per-source counts to the `status` CLI summary (e.g. *"opec_rss: 3 events seen, 1 alert sent"*).
- Manual smoke test (document in README): enable each source, run `test-rss <name>`, confirm at least one feed entry is fetched.
- Integration test: scripted RSS feeds + scripted price action → assert correct alert is produced.

**Acceptance:**
- `python -m oilbot test-rss opec_rss` prints recent OPEC press releases (or "no recent items" if none).
- Same for `google_news_oil` and `nhc_atlantic`.
- Setting `NEWS_SOURCES=truth_social,opec_rss,google_news_oil,nhc_atlantic` and running `python -m oilbot run` boots without errors and logs `news_received` events from at least `google_news_oil` within ~5 min during US trading hours.
- Tests green.

> Note: NOAA's feed will be quiet outside hurricane season — that's expected. Don't treat zero events as a bug.

---

## Phase 14 — EIA Release listener

**Goal:** scheduled, structured releases (weekly petroleum + rig count).

**Tasks:**
- Implement `oilbot/news/eia.py` with `EIAReleaseListener` per SPEC_V2 §4.3.
- HTTP client over `httpx.AsyncClient` to api.eia.gov; API key from `.env` (`EIA_API_KEY`).
- Two configured releases: weekly petroleum stocks (Wednesdays 10:30 ET) and Baker Hughes rigs (Fridays 13:00 ET). Define them in `configured_sources.py`.
- Polling strategy: idle until 5 min before scheduled release time; then poll every 30 s for up to 30 min until new data appears or window closes.
- Compute deltas vs. previous week. Emit `EIAReleaseEvent` (subclass of `NewsEvent`) carrying the metric and delta.
- Extend `HybridClassifier`: structured-event branch per SPEC_V2 §4.6. Pure threshold logic from `config/strategy.yaml`.
- Add `test-eia` CLI subcommand: fetches latest available release and prints the parsed result and what classification it would receive.
- Unit tests with mocked HTTP responses for: clear bullish draw, clear bearish build, threshold-grazing data (returns `NEUTRAL`), API error (logs cleanly).

**Acceptance:**
- `python -m oilbot test-eia` returns last week's crude draw/build with a sensible classification.
- Tests green; classification thresholds tunable in `config/strategy.yaml`.
- Manual smoke test (only possible on Wed mornings): run the bot from 10:25 ET; observe `eia_release_received` and `classification` events around 10:30 ET.

> Heads-up: EIA occasionally releases on Thursdays during US holiday weeks. Schedule should be data-driven from the EIA calendar endpoint if available; otherwise document the manual override.

---

## Phase 15 — Calendar listener (FOMC + NFP)

**Goal:** awareness of known volatility windows.

**Tasks:**
- Implement `oilbot/news/calendar.py` with `CalendarListener` per SPEC_V2 §4.4.
- FOMC dates: scrape and parse `https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm` once on startup; cache in `data/calendar.json`; refresh weekly.
- NFP dates: computed deterministically (first Friday of each month at 8:30 ET) — no fetching needed.
- Emit `event="watch"` 5 minutes before, `event="released"` 30 seconds after each scheduled time. Both labeled `NEUTRAL`.
- These events do *not* trigger the strategy engine's full window. They're informational only — logged and optionally Telegram-noted as `system` messages ("FOMC release in 5 minutes — heads up").
- Unit tests with frozen `datetime.now()`.

**Acceptance:**
- `python -m oilbot test-calendar` prints the next 5 FOMC and NFP dates and times.
- During a manual test with mocked time, the bot fires the "watch" and "released" system messages at the right moments.
- Tests green.

---

## Phase 16 — Price confirmation context

**Goal:** correlated-asset confirmation before firing alerts.

**Tasks:**
- Add `yfinance` to the dependency list (`uv add yfinance`).
- Implement `oilbot/price/context.py` with `PriceContext` per SPEC_V2 §4.5. Five tickers: `BZ=F`, `CL=F`, `DX-Y.NYB`, `XLE`, `XOP`.
- Poll each ticker every 60 s, store last 5 minutes of prices, expose `is_aligned(side)` returning `aligned | mixed | disaligned` plus per-ticker direction.
- Add `CONFIRMATION_MODE` to config (env: `off | advisory | required`).
- Wire `PriceContext` into the orchestrator and inject into `StrategyEngine`. Update the engine's alert-emission path: per-mode behavior from SPEC_V2 §3.7.
- Extend `Alerter.alert(payload)` to include the confirmation block in the formatted message when mode != off.
- Add `confirmation_status` column to `alerts` table; backfill `'not_checked'` for existing rows on startup.
- Add `test-confirmation` CLI subcommand: prints current alignment for `BUY` and `SELL`.
- Unit tests with a fake price source for the confirmation logic in all three modes.

**Acceptance:**
- `python -m oilbot test-confirmation` prints something like `BUY: aligned (Brent ↑, DXY ↓, XLE ↑); SELL: disaligned`.
- A manual `dev-trigger BULLISH` produces a Telegram alert with the confirmation footer (in `advisory` mode).
- In `required` mode, an event whose confirmation comes back `disaligned` produces a `system` Telegram message instead of an alert.
- Tests green.

---

## Phase 17 — Wiring + integration test + polish

**Goal:** end-to-end v2.

**Tasks:**
- Update the integration test from v1 to script an EIA release with a 5-million-barrel draw + price spike-pullback-breakout, asserting one `BULLISH` alert with `confirmation_status` populated.
- Add a second integration test for an OPEC RSS event.
- Update README: new sources table, `EIA_API_KEY` setup, `NEWS_SOURCES` allow-list, `CONFIRMATION_MODE` explanation, refreshed CLI reference.
- Update the Makefile if any new targets are useful (`make test-eia`, `make test-rss`).
- Final ruff + mypy sweep.

**Acceptance:**
- `make lint test` clean.
- New integration tests pass.
- README walks a reader through enabling each new source.

---

## Decisions baked into this plan

- **Free only.** Every endpoint above can be hit without a paid subscription. EIA requires a free API key (no card, no payment).
- **Modular.** Each new source is a separate listener that emits `NewsEvent` (or a subclass). The classifier and strategy engine from v1 don't change.
- **Tunable.** All thresholds (EIA draw size, rig count change, confirmation mode) live in config so behavior can be adjusted without code changes.
- **Backwards-compatible.** With `NEWS_SOURCES=truth_social` and `CONFIRMATION_MODE=off`, the bot behaves exactly like v1.
- **Optional gating.** Confirmation defaults to `advisory` — informational, doesn't suppress alerts. Users can opt into `required` after observing a baseline.

## Stretch / v3 ideas (not in this plan)

- **X (Twitter) API** — when budget allows. Architecture supports it (just another `NewsSource`).
- **Backtesting** — replay historical EIA + RSS + price data to tune thresholds.
- **Per-source `default_sentiment` overrides via classification metrics** — learn which sources produce the most accurate alerts and reweight.
- **A proper schedule library** for calendar listener — current implementation is hand-rolled, fine for FOMC/NFP but if you add more events, consider `apscheduler`.
- **Multi-instrument** — Brent (BZ), heating oil (HO), nat gas (NG). The PriceMonitor already takes a contract; extending is mostly config.
