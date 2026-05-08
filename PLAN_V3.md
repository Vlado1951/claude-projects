# Implementation Plan — v3 Forward-Test Framework

This plan picks up at **Phase 18**, after v2 is shipped. Five phases, each sized for a single Claude Code session.

## How to use this plan with Claude Code

1. `cd` into your existing project (Phases 0–17 already on `main`, with `v2.0.0` tag).
2. Drop `SPEC_V3.md` and `PLAN_V3.md` at the project root next to the v1/v2 docs.
3. Run `claude` and start with: *"Read `SPEC_V3.md` and `PLAN_V3.md`. v2 is complete on `main`. We're starting Phase 18."*
4. After each phase: run acceptance checks, commit, then proceed.

---

## Phase 18 — Schema + alert tracking on fire

**Goal:** every fired alert gets a tracking row inserted; no behavior change yet beyond persistence.

**Tasks:**
- Add the `alert_outcomes` table per SPEC_V3 §4.2; create-if-not-exists migration on startup.
- New `oilbot/persistence/outcomes.py` module with thin DB helpers: `open_outcome(alert_id, ...)`, `update_outcome(...)`, `close_outcome(...)`, `list_open()`, `list_closed(since=...)`, `get_by_alert(alert_id)`.
- In the `StrategyEngine`, immediately after `Alerter.alert(payload)` returns, call `outcomes.open_outcome(alert_id, payload, tracked_from_at=now)`.
- Add `OUTCOME_TIMEOUT_MINUTES`, `FILL_MODEL`, `STOP_SLIPPAGE_TICKS`, `TICK_SIZE`, `OUTCOME_TELEGRAM` to `Config` with sensible defaults (SPEC_V3 §5).
- Unit tests: opening, updating, closing outcomes; querying open vs closed; uniqueness on `alert_id`.

**Acceptance:**
- `uv run pytest` — all green.
- `make lint test` clean.
- A manual `dev-trigger BULLISH` that produces an alert results in one new row in `alert_outcomes` with status `tracking`. Verify with:
  ```
  sqlite3 data/bot.db "SELECT alert_id, status FROM alert_outcomes ORDER BY id DESC LIMIT 5;"
  ```

> No tracking logic yet — this phase only establishes the persistence side. The row will sit at `tracking` forever until Phase 19 lands.

---

## Phase 19 — OutcomeTracker

**Goal:** alerts actually resolve to `target_hit` / `stop_hit` / `timeout`.

**Tasks:**
- Implement `oilbot/outcomes/tracker.py` with the `OutcomeTracker` class per SPEC_V3 §4.1.
- Wire it into the orchestrator alongside the existing background tasks. It receives the same `PriceMonitor` instance the strategy engine uses.
- Wire `StrategyEngine.fire_alert` so it also calls `outcome_tracker.open_alert(alert_id, payload)` for in-memory tracking.
- On orchestrator startup, `OutcomeTracker.start()` loads `WHERE status='tracking'` from DB and resumes (per SPEC_V3 §7).
- Force-close stale rows older than `2 × OUTCOME_TIMEOUT_MINUTES` as `timeout` with realized_r computed from last available price (`PriceMonitor.last_price`); if no price available, set realized_r to NULL and log a warning.
- Pause logic on IBKR disconnect (track `paused_seconds`).
- Optional outcome Telegram per `OUTCOME_TELEGRAM` config flag — formatted per SPEC_V3 §6.
- Structured logs per SPEC_V3 §8.
- Unit tests with a fake PriceMonitor that emits a scripted tick sequence:
  - BUY hits target before stop → `target_hit`, realized_r ≈ 1.5.
  - BUY hits stop before target → `stop_hit`, realized_r ≈ -1.0.
  - BUY hits neither within timeout → `timeout`, realized_r computed from last price.
  - Mirror cases for SELL.
  - `next_tick` vs `theoretical` fill model produces different fill prices for the same tick sequence.
  - Stop slippage of 1 tick produces realized_r slightly worse than -1.
  - MFE / MAE updated correctly across the tick sequence.
  - Disconnect mid-tracking → resume on reconnect → `paused_seconds` non-zero.
  - Restart-recovery: pre-seed a `tracking` row in DB; start orchestrator; assert tracker resumes it.

**Acceptance:**
- `uv run pytest` — all green.
- `make lint test` clean.
- Manual: `dev-trigger BULLISH` — if a setup completes, the resulting alert resolves within `OUTCOME_TIMEOUT_MINUTES` to one of the three terminal states. Verify with:
  ```
  sqlite3 data/bot.db "SELECT alert_id, status, realized_r, mfe_r, mae_r FROM alert_outcomes ORDER BY id DESC LIMIT 5;"
  ```
  At least one row should now be in a terminal state (or, if you ran it during a quiet period, the row will eventually show `timeout` after the configured minutes).

---

## Phase 20 — `performance` CLI

**Goal:** human-readable summary of what the tracker has recorded.

**Tasks:**
- Implement `oilbot/cli/performance.py` per SPEC_V3 §4.3.
- Argparse: `--since <duration>` (e.g. `7d`, `24h`, `30d`), `--by <dimension>` (`source`, `sentiment`, `confirmation`, `hour`).
- Aggregations from SPEC_V3 §4.3: total, hit rate, stop rate, timeout rate, avg/median R, profit factor, avg MFE/MAE, best/worst trade.
- Pretty text output: aligned columns, one-line summary at top, breakdown table below.
- Insufficient-sample message when N<5.
- Wire `oilbot performance` into `__main__.py` argparser.
- Unit tests: insert a known set of outcomes, run aggregation, assert numeric output matches.

**Acceptance:**
- `uv run python -m oilbot performance` prints a sensible summary (or "insufficient sample size" on a fresh DB).
- `uv run python -m oilbot performance --since 7d --by source` runs without error.
- Tests green.

---

## Phase 21 — HTML report

**Goal:** a shareable static report file.

**Tasks:**
- Implement `oilbot/cli/performance.py`'s `--html <path>` option.
- Single self-contained HTML file. Inline CSS, Chart.js loaded from CDN (no JS dependencies in the project).
- Sections: header summary, breakdown by source / sentiment / confirmation, R-multiple bar chart per trade (Chart.js), sortable HTML table of all closed alerts with columns: `alert_id, fired_at, source, side, entry, stop, target, exit, realized_r, status, mfe_r, mae_r`.
- Sortable table via small inline JS (no framework — just `addEventListener` on `<th>` clicks).
- Responsive enough to read on a phone.
- Unit test that the file is written and is well-formed HTML (parse with `html.parser` and assert no exceptions).

**Acceptance:**
- `uv run python -m oilbot performance --html /tmp/report.html` produces a file.
- `open /tmp/report.html` shows the report. The chart renders. The table sorts.
- Tests green.

---

## Phase 22 — Integration test + polish

**Goal:** end-to-end confidence and v3 docs.

**Tasks:**
- Integration test in `tests/test_integration_v3.py`: scripted bullish news → scripted price sequence (spike → pullback → breakout → continuation → target hit) → assert one alert fired AND one `target_hit` outcome with realized_r ≈ 1.5.
- Add a second integration test for `stop_hit` outcome.
- README updates: new section "Forward-test mode" explaining the `alert_outcomes` table, `performance` command, the four config knobs, and how to interpret R-multiples and profit factor for someone without a trading background.
- Makefile: add `make performance` target as a shortcut.
- Final ruff + mypy sweep.

**Acceptance:**
- `make lint test` clean.
- `make performance` works (even if it just prints "insufficient sample size" on a fresh install).
- README covers the forward-test workflow end-to-end.

---

## Decisions baked into this plan

- **Forward-test only.** No historical replay. v4 territory.
- **R-multiples, not dollars.** Keeps the framework useful regardless of position size; dollar P&L can be derived later by multiplying.
- **Honest defaults.** `next_tick` fill model and zero slippage by default — the operator can dial in pessimism via `STOP_SLIPPAGE_TICKS` if they want.
- **Resume on restart.** The `OutcomeTracker` survives bot restarts. No tracking gets silently abandoned.
- **Optional Telegram noise.** Outcome notifications default off — most operators want only entry alerts in real time and the rest in `performance` reviews.

## What this gives you operationally

After running this for ~2–4 weeks of live operation:
- You'll have a real number for hit rate. If it's 60%+ on closed alerts with positive average R, the strategy is worth following with real money.
- Per-source breakdown will tell you which news source produces the bot's best alerts. You may decide to disable a source that consistently scores below break-even.
- MFE_R consistently exceeding realized_r tells you targets are too far / management could be tighter. The opposite says targets are conservative.
- Hit rate by sentiment (bullish vs bearish) reveals direction bias in either the classifier or the underlying market regime.

## Stretch / v4 ideas (not in this plan)

- **Historical backtester.** Replay archived news + historical 5-second MCL bars. Iterate on strategy params in minutes instead of weeks.
- **Walk-forward parameter tuning.** Use the backtester to optimize `SPIKE_WINDOW_SECONDS`, `PULLBACK_K`, etc., without overfitting.
- **Position sizing model.** Once strategy is validated, add a Kelly-fraction or fixed-percent sizer to translate R-multiples into trade size.
- **Live IBKR paper-trade execution.** Wire the alerts into actual paper orders. Then real orders once you trust it.
- **Telegram performance digest.** A scheduled "weekly performance" Telegram message summarizing the last 7 days.
