# Implementation Plan — Oil News Trading Alert Bot

This plan is written for Claude Code. Hand it `SPEC.md` and this file. Each phase has a clear goal, the files to touch, and acceptance criteria. Don't move to phase N+1 until phase N's acceptance is met.

## How to use this plan with Claude Code

1. `cd` into an empty project directory.
2. Drop `SPEC.md` and `PLAN.md` at the root.
3. Run `claude` and start with: *"Read SPEC.md and PLAN.md. We're starting Phase 0."*
4. After each phase, ask Claude to summarize what changed and confirm acceptance criteria are met before continuing.

Phases are sized so each one fits comfortably in a single Claude Code session.

---

## Phase 0 — Project skeleton

**Goal:** an empty but well-shaped Python project that runs.

**Tasks:**
- Initialize `pyproject.toml` with `uv` (preferred) or `pip-tools`. Python 3.11+.
- Add dev dependencies: `pytest`, `pytest-asyncio`, `ruff`, `mypy`.
- Add runtime deps placeholders: `httpx`, `ib_insync`, `pydantic`, `pydantic-settings`, `structlog`, `pyyaml`, `anthropic`, `openai`.
- Create the package layout:
  ```
  oilbot/
    __init__.py
    __main__.py        # CLI dispatch
    config.py          # pydantic-settings Config
    logging_setup.py
    cli/
      run.py
      test_telegram.py
      test_ibkr.py
      classify.py
      status.py
    news/
      __init__.py
      listener.py      # NewsListener
      models.py        # NewsEvent
    classifier/
      __init__.py
      hybrid.py
      keywords.py
      llm.py
      keywords.yaml
    price/
      __init__.py
      monitor.py       # PriceMonitor
    strategy/
      __init__.py
      engine.py        # StrategyEngine
      window.py
    alerts/
      __init__.py
      telegram.py
      formatter.py
    persistence/
      __init__.py
      db.py
    orchestrator.py
    types.py           # Sentiment enum, Classification, AlertPayload, etc.
  config/
    strategy.yaml
  data/                # gitignored
  tests/
  ```
- `__main__.py` dispatches the subcommands listed in SPEC §8 (stubs that print "not implemented yet" are fine).
- `.env.example` with all keys from SPEC §5.
- `.gitignore` covering `.env`, `data/`, `__pycache__`, `.venv`.
- `README.md` with setup instructions (`uv sync`, copy `.env.example` to `.env`, run `python -m oilbot test-telegram`).

**Acceptance:**
- `python -m oilbot` prints help with all 5 subcommands listed.
- `pytest` runs and reports 0 tests, 0 failures.
- `ruff check .` and `mypy oilbot` pass on the empty stubs.

---

## Phase 1 — Config + logging + CLI dispatch

**Goal:** real config loading and a working logger; subcommands wired up to handler functions (still stubs internally).

**Tasks:**
- Implement `config.py` with a `Config` class via `pydantic-settings`. Validate on load (e.g. `LLM_API_KEY` required when `LLM_PROVIDER != "none"`).
- Implement `logging_setup.py` with `structlog`, JSON output, level from config.
- Implement CLI dispatch in `__main__.py` using `argparse` (one subparser per subcommand). No third-party CLI lib needed.
- Each subcommand handler in `cli/` accepts a `Config` and runs an async function.

**Acceptance:**
- Bad `.env` (missing required key) produces a clear error and non-zero exit.
- Good `.env` followed by `python -m oilbot run` logs a structured "starting" line and exits cleanly (Ctrl-C).
- `python -m oilbot classify "test"` prints "not implemented" but doesn't crash.

---

## Phase 2 — Telegram alerter

**Goal:** can send messages.

**Tasks:**
- Implement `alerts/telegram.py` with `TelegramAlerter` class, methods `alert`, `system`, `startup`.
- Implement `alerts/formatter.py` for the alert payload format from SPEC §4.5. Helper `format_alert(payload: AlertPayload) -> str`.
- Wire `cli/test_telegram.py` to send "Telegram wiring OK ✅" (escape special chars for MarkdownV2).
- Unit test the formatter with a sample payload.

**Acceptance:**
- `python -m oilbot test-telegram` delivers a message to the configured chat.
- Formatter unit test passes.
- Telegram 4xx is logged and does not crash.

---

## Phase 3 — Classifier (keywords first)

**Goal:** stage-1 keyword classifier works end to end.

**Tasks:**
- Author `classifier/keywords.yaml` with the lists from SPEC §4.2 (use those as a starting set; expand as you go).
- Implement `classifier/keywords.py` with `classify_keywords(text: str) -> Classification | None` (returns `None` when ambiguous → fall through to LLM).
- Implement `classifier/hybrid.py` with `HybridClassifier.classify(event)`. For now, when stage-1 returns `None`, default to `NEUTRAL` (LLM fallback in next phase).
- Wire `cli/classify.py`.
- Unit tests covering: clear bullish, clear bearish, both-keywords (→ neutral fallback), no-keywords (→ neutral fallback), case-insensitivity, URL stripping.

**Acceptance:**
- `python -m oilbot classify "OPEC announces production cut"` → BULLISH.
- `python -m oilbot classify "US releases strategic petroleum reserve"` → BEARISH.
- `python -m oilbot classify "Good morning everyone"` → NEUTRAL.
- Tests pass.

---

## Phase 4 — Classifier (LLM fallback)

**Goal:** stage-2 fallback works for ambiguous cases.

**Tasks:**
- Implement `classifier/llm.py` with two adapters (Anthropic, OpenAI) behind a common `LLMClassifier` interface.
- Strict JSON output prompt (see SPEC §4.2). Parse defensively; on any parse failure return `NEUTRAL` with `confidence=0.0`.
- Cache layer: read/write to `classifications` table. If cached, skip the call.
- Wire into `HybridClassifier`.
- Unit tests with a mocked HTTP layer (do not hit real APIs in tests).

**Acceptance:**
- `python -m oilbot classify "<ambiguous text>"` returns a non-neutral result with rationale when the LLM is configured.
- Re-running the same classification on the same text shows `source=cache` in logs.
- Tests pass without network access.

---

## Phase 5 — Persistence

**Goal:** SQLite-backed dedupe and audit trail.

**Tasks:**
- Implement `persistence/db.py` with a thin wrapper: `init_schema`, `mark_seen(post_id)`, `is_seen(post_id)`, `record_classification(...)`, `record_alert(...)`, `last_24h_summary()`.
- Schema migrations are not needed for v1; just `CREATE TABLE IF NOT EXISTS`.
- Wire `cli/status.py` to `last_24h_summary`.
- Unit tests using a temp DB path.

**Acceptance:**
- `python -m oilbot status` prints zero counts on a fresh DB.
- Tests pass.

---

## Phase 6 — News listener (Truth Social)

**Goal:** poll Truth Social and emit `NewsEvent`s.

**Tasks:**
- Implement `news/listener.py`. Investigate the public profile page (HTML or JSON-in-script-tag) and isolate parsing in `_parse_feed`.
- Use `httpx.AsyncClient` with realistic headers and a configurable poll interval (default 30s).
- On startup, fetch once and mark all current post IDs as "seen" without emitting (so we don't fire on history).
- Emit only newer posts on subsequent polls.
- Persist seen IDs via `persistence.db`.
- Backoff on HTTP errors (exponential, capped at 5 min).
- On parse failure, log and send a one-time degraded-mode Telegram message.
- Add a `--once` debug flag to the listener (run one poll and exit) for manual testing.

**Acceptance:**
- A short-lived test run (`python -m oilbot run --once-news`, or via a small test script) prints the latest visible post.
- Restart does not re-emit posts already seen.
- Forcing a parse error (e.g. patching the parser to raise) logs cleanly and triggers the degraded Telegram message exactly once.

> **Note for the implementer:** Truth Social's frontend changes. The parser will need verification at the time of implementation. If the structure has changed since SPEC was written, update `_parse_feed` only and document the new selectors in a comment.

---

## Phase 7 — IBKR price monitor

**Goal:** live MCL ticks and bars in a rolling buffer.

**Tasks:**
- Implement `price/monitor.py` using `ib_insync`.
- Resolve the front-month MCL contract on startup via `reqContractDetails`. Log the chosen contract month.
- Subscribe to 5-second bars (kept 30 min) and ticks (kept 5 min).
- Auto-reconnect on disconnect with backoff. Send Telegram `system` messages on disconnect/reconnect.
- Wire `cli/test_ibkr.py` to connect, log the front-month contract, stream prices for 30 s, then disconnect cleanly.

**Acceptance:**
- `python -m oilbot test-ibkr` (with TWS paper running) prints the contract and ≥ 5 price updates over 30 s.
- Killing TWS mid-run produces a "feed disconnected" Telegram message; restarting TWS produces a "feed reconnected" message.
- Missing data subscription produces the documented helpful error.

---

## Phase 8 — Strategy engine

**Goal:** decide alerts from news + price.

**Tasks:**
- Implement the state machine in `strategy/engine.py` per SPEC §4.4.
- `strategy/window.py` holds pure helpers (find spike high, detect pullback, detect breakout) — these are the unit-test targets.
- `StrategyEngine.handle(event, classification)` spawns one async task per non-neutral event. Cap concurrency at 5 with a semaphore.
- ATR computation over the rolling buffer; static fallback if too few bars.
- On breakout/breakdown: build `AlertPayload`, call `Alerter.alert`, record in DB.
- On window timeout: log "no setup", record nothing.

**Tests:**
- A fake `PriceMonitor` lets tests inject a list of bars.
- Cover: bullish breakout fires, bullish but no pullback (timeout), bullish pullback then no breakout (timeout), bearish breakdown fires, bearish but breakout reverses (timeout). Mirror cases.
- Test the concurrency cap with 6 simultaneous events.

**Acceptance:**
- All strategy unit tests pass.
- Manual smoke test: feed a fake bullish event into a running bot with the live price feed; observe either an alert or a clean "no setup" log.

---

## Phase 9 — Orchestrator + integration test

**Goal:** all components running together.

**Tasks:**
- Implement `orchestrator.py` that constructs everything from `Config`, opens the DB, starts the price monitor, starts the news loop, and routes events through the classifier into the strategy engine.
- Clean shutdown on SIGINT/SIGTERM: stop accepting new events, drain in-flight strategy tasks (with a timeout), disconnect IBKR, close DB.
- Wire `cli/run.py`.
- Integration test in `tests/test_integration.py` using fakes for news + price + telegram. Script: bullish post → spike → pullback → breakout. Assert exactly one alert was sent with expected entry/stop/target.

**Acceptance:**
- Integration test passes.
- `python -m oilbot run` boots, sends the startup Telegram message, runs for ≥ 1 minute without errors, and shuts down cleanly on Ctrl-C.

---

## Phase 10 — Polish

**Goal:** ship-ready.

**Tasks:**
- Fill out `README.md`: setup, IBKR data subscription note, how to run as a service (`systemd` unit example or `pm2` snippet).
- Add a `Makefile` or `justfile` with `lint`, `test`, `run`.
- Verify `ruff` and `mypy` clean across the codebase.
- Document the manual smoke-test checklist in `README.md`.

**Acceptance:**
- `make lint test` passes.
- A new user can follow the README from a clean clone to a running bot in under 15 minutes (assuming TWS, Telegram bot, and LLM key already provisioned).

---

## Stretch / v2 ideas (not part of this plan)

These are explicitly out of scope for v1 but noted so they don't get accidentally bolted on mid-build:
- IBKR paper-trade order placement.
- Backtesting framework against historical price + news.
- Multiple news sources (X API, RSS, oil-specific outlets).
- Web dashboard.
- Notification channels beyond Telegram.

---

## Decisions baked into this plan (reflecting the choices made)

- **News source:** Truth Social scraping. Listener is isolated so it can be replaced if the site breaks.
- **Trading scope:** alerts only. No order placement code anywhere.
- **Price feed:** IBKR via `ib_insync`. Paper port (7497) by default.
- **Classifier:** hybrid — keywords first, LLM fallback, NEUTRAL on ambiguity or LLM error.

If any of these change later, the affected components are scoped to a single file each (`news/listener.py`, `price/monitor.py`, `classifier/hybrid.py`).
