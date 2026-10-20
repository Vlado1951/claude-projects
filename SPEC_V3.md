# Oil News Trading Alert Bot — v3 Specification

This document extends `SPEC.md` (v1) and `SPEC_V2.md` (v2). It adds **forward-test instrumentation** so the operator can measure the bot's prediction accuracy on real prices before risking capital. **Read v1 and v2 first.** Anything not changed here behaves exactly as in v2.

## 1. Goal

Answer the question: *"If I had taken every alert this bot fired, would I be profitable?"*

The bot already produces BUY/SELL alerts with a stated entry, suggested stop, and suggested target. v3 adds the missing piece: track what *actually* happens to MCL after each alert, score the outcome in standardized R-multiples, and surface aggregate stats so the operator can decide whether the strategy is good enough to follow.

This is **forward-test** instrumentation only — historical backtesting is out of scope and deferred to v4.

## 2. Scope

### In scope
- A new `OutcomeTracker` async task that watches MCL ticks for every fired alert and records the outcome.
- A new `alert_outcomes` table in SQLite for per-alert results.
- A `performance` CLI subcommand that aggregates outcomes into stats.
- An HTML/markdown export option for nicer reporting.
- Resume-on-restart: if the bot restarts mid-window, it picks up tracking already-open alerts.
- Configurable assumptions: outcome timeout horizon, fill model, slippage tick count.

### Out of scope (v3)
- Actual order placement (still alerts-only).
- Dollar-denominated P&L (R-multiples only — no position sizing).
- Historical backtesting against past data.
- Strategy parameter optimization based on outcomes (manual interpretation only).
- Web dashboard (CLI text + optional static HTML report only).

## 3. Concepts

### 3.1 Alert lifecycle (v3)

In v2, alerts were fire-and-forget. v3 adds states:

```
[fire] → tracking → (target_hit | stop_hit | timeout) → closed
```

When an alert fires:
1. v2 behavior: row inserted into `alerts` table, Telegram message sent.
2. v3 addition: row inserted into `alert_outcomes` with status `tracking`.

The `OutcomeTracker` then watches MCL ticks until one of the three terminal conditions is met.

### 3.2 R-multiple

The standardized profit/loss unit. For a `BUY` alert with entry `E`, stop `S`, exit `X`:

```
R = (X - E) / (E - S)
```

For `SELL`, signs flip. R = +1 means hit target as planned. R = -1 means hit stop as planned. R = +1.5 means exited at 1.5× the planned risk in the favorable direction.

R-multiples normalize across alerts of different sizes — a $0.50 winner on a tight setup is the same R as a $2.00 winner on a wide setup, when expressed in units of risk.

### 3.3 MFE / MAE

- **MFE** (Maximum Favorable Excursion): the most favorable price observed during the tracking window. For a BUY, the highest price reached.
- **MAE** (Maximum Adverse Excursion): the most adverse price observed. For a BUY, the lowest price reached.

Both expressed in R units. Useful diagnostics:
- High MFE_R but low realized_R → targets are too far / management too tight.
- Low MAE_R but realized_R = -1 → stops too tight / poorly placed.

### 3.4 Fill model

Two configurable assumptions about entry:

- **`theoretical`** — assume the alert's stated breakout price was filled exactly. Optimistic. Useful baseline.
- **`next_tick`** — first observed tick at or beyond the breakout price after the alert fires. More realistic.

Default: `next_tick`. The difference matters — if the bot fires on a fast move, `theoretical` may price the entry below where you could realistically have entered.

### 3.5 Slippage on stops

Real-world stop fills tend to be worse than the stated stop, especially on fast moves. v3 adds an optional `stop_slippage_ticks` config (default 0 — assume perfect stop fill). Setting it to `1` or `2` ticks (`$0.01–$0.02` for MCL) makes the test more honest.

## 4. Components

### 4.1 `OutcomeTracker`

A new async component, owned by the orchestrator.

**Interface:**
```python
class OutcomeTracker:
    async def start() -> None              # spawned as background task
    def open_alert(alert_id: int, payload: AlertPayload) -> None
    @property
    def open_count(self) -> int
```

**Behavior:**
- On startup, queries `alert_outcomes WHERE status='tracking'` and resumes tracking each one. (A bot restart shouldn't lose in-flight outcome tracking.)
- Subscribes to `PriceMonitor`'s tick stream.
- For every tick, iterates open alerts and checks:
  - Has the configured fill price been touched yet (if `next_tick` mode)? If yes, mark filled and start tracking from there.
  - Is the current price ≥ target (BUY) or ≤ target (SELL)? → `target_hit`.
  - Is the current price ≤ stop (BUY) or ≥ stop (SELL)? → `stop_hit`.
  - Has `now > tracked_from + outcome_timeout`? → `timeout`.
- On any terminal condition: writes the row to `alert_outcomes` with final fields, removes from in-memory open set.
- Updates MFE/MAE on every tick regardless.

**Tick handling efficiency:** for N open alerts, each tick is O(N) work. Capped at 5 concurrent in-flight alerts (already the strategy engine's cap), so this is bounded and cheap.

**Edge cases:**
- Bot disconnects from IBKR mid-tracking: pause tracking (don't time out alerts during disconnects); resume when reconnected. Add a `paused_seconds` tracking metric so timeout still uses wall clock minus disconnect time.
- Alert fires during low-liquidity hours: tracking continues; if price never touches anything, timeout fires.
- Multiple alerts open with overlapping levels: handled independently.

### 4.2 `alert_outcomes` table

```sql
CREATE TABLE alert_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_id INTEGER NOT NULL UNIQUE REFERENCES alerts(id),
    status TEXT NOT NULL CHECK (status IN ('tracking','target_hit','stop_hit','timeout')),
    tracked_from_at TIMESTAMP NOT NULL,
    tracked_until_at TIMESTAMP,
    fill_price REAL,
    fill_at TIMESTAMP,
    exit_price REAL,
    exit_at TIMESTAMP,
    mfe_price REAL,
    mae_price REAL,
    mfe_r REAL,
    mae_r REAL,
    realized_r REAL,
    paused_seconds INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_outcomes_status ON alert_outcomes(status);
```

Migration at startup creates the table if it doesn't exist. No backfill of existing alerts — they were never tracked, so they remain untracked. Document this clearly.

### 4.3 `performance` CLI

```
oilbot performance [--since <duration>] [--by <dimension>] [--html <path>]
```

Examples:
- `oilbot performance` — all-time text summary.
- `oilbot performance --since 7d` — last 7 days.
- `oilbot performance --by source` — breakdown per news source (`truth_social`, `eia_petroleum`, etc.).
- `oilbot performance --by sentiment` — bullish vs bearish.
- `oilbot performance --by confirmation` — `aligned` vs `mixed` vs `disaligned` (if confirmation mode is on).
- `oilbot performance --html report.html` — write a static HTML report.

**Stats produced (for a given slice):**
- Total alerts in slice.
- Closed alerts (excluding still-tracking).
- Hit rate = `target_hit / closed`.
- Stop rate = `stop_hit / closed`.
- Timeout rate = `timeout / closed`.
- Average R (mean of `realized_r` across closed).
- Median R.
- Profit factor = `sum(realized_r where realized_r > 0) / abs(sum(realized_r where realized_r < 0))`.
- Average MFE_R, average MAE_R.
- Best trade (max realized_r) and worst (min).

Edge cases:
- Fewer than 5 closed alerts → print "insufficient sample size, more data needed."
- Zero alerts in the slice → friendly message, exit 0.

### 4.4 HTML report

A single self-contained HTML file: a header summary, the breakdown table, a small Chart.js bar chart of R-multiples per trade, and a sortable trades table.

No live JavaScript fetching — purely static, generated from the SQLite at the moment the command runs. Open with `open report.html` to view.

## 5. Configuration

New keys in `.env` (all optional with sensible defaults):

```
OUTCOME_TIMEOUT_MINUTES=60          # how long to follow an alert before timeout
FILL_MODEL=next_tick                # next_tick | theoretical
STOP_SLIPPAGE_TICKS=0               # 0 = no slippage; 1-2 for realistic
TICK_SIZE=0.01                      # MCL minimum tick (kept generic for future instruments)
```

The defaults represent the most honest measurement — a `next_tick` fill, no slippage, and a 1-hour observation window after fill.

## 6. Telegram message changes

When an alert fires, the existing Telegram message is unchanged.

When an outcome resolves, optionally send a follow-up Telegram message with the result:

```
📊 Alert outcome — BUY MCL
Triggered: 14:07:32 UTC @ 78.45
Result: target_hit (1.5R) at 14:23:14 UTC
MFE: 1.62R, MAE: -0.34R
```

Controlled by config `OUTCOME_TELEGRAM=on|off`, default `off` (avoids alert noise). When `on`, useful for live debugging. Always written to logs regardless.

## 7. Persistence — schema migration

The startup migration logic from v1/v2 already handles "create table if not exists" pattern. v3 adds `alert_outcomes` to that list. No changes to existing tables.

When the bot restarts:
1. Migration creates `alert_outcomes` if missing.
2. `OutcomeTracker.start()` loads `WHERE status='tracking'` rows.
3. For each, validates that the alert is still trackable (price feed connected, alert not too old). If older than `2 × OUTCOME_TIMEOUT_MINUTES`, force-close as `timeout` with `realized_r` computed from last known prices. Otherwise resume.

## 8. Logging additions

New structured log events:
- `outcome_tracking_started` — alert opened for tracking.
- `outcome_filled` — fill price observed (next_tick mode).
- `outcome_resolved` — terminal condition reached, with status and realized_r.
- `outcome_paused` — IBKR disconnect while tracking.
- `outcome_resumed` — reconnect.

## 9. Testing

- **Unit tests** for the outcome math (R-multiple computation, MFE/MAE under various tick sequences).
- **Unit tests** for terminal condition detection (target hit before stop, stop hit first, timeout, simultaneous touch resolved by tick order).
- **Unit tests** for slippage and fill model.
- **Integration test**: scripted alert + scripted price sequence reaching target → assert `outcome_resolved` with status `target_hit` and correct R.
- **Restart test**: write a `tracking` row to the DB, start the orchestrator, assert `OutcomeTracker` resumes tracking that row.
- **Performance CLI tests**: insert known outcomes, run the command, assert text output contains expected stats.

## 10. What's deliberately not in v3

- **Backtesting against historical data.** Different problem; needs historical news + price archives. Deferred to v4.
- **Position sizing / dollar P&L.** R-multiples are sufficient for "is this strategy worth following." Add dollars only when going live.
- **Multi-target / scaling out.** Each alert has one target. Modeling partial exits is more complex than v3 needs.
- **Strategy parameter auto-tuning.** v3 surfaces stats; the operator interprets them and decides what to tweak. No automatic optimization loop.

## 11. What "done" looks like for v3

- A new alert fires → row appears in `alert_outcomes` with status `tracking` immediately.
- Within `OUTCOME_TIMEOUT_MINUTES`, the row resolves to `target_hit`, `stop_hit`, or `timeout` with sensible MFE/MAE/R values.
- `oilbot performance` runs without errors and prints stats (or "insufficient sample size" if N<5).
- `oilbot performance --html report.html` writes a viewable file.
- Bot restart while alerts are tracking does not lose in-flight tracking.
- `OUTCOME_TIMEOUT_MINUTES`, `FILL_MODEL`, `STOP_SLIPPAGE_TICKS` are all respected.
- Tests pass. ruff and mypy clean.
