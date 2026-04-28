# Oil News Trading Alert Bot — Specification

## 1. Purpose

A Python application that watches a single source of oil-relevant news (Donald Trump's Truth Social feed), classifies each new post as bullish, bearish, or neutral for crude oil, then watches the post-news price action on IBKR's Micro Crude Oil (MCL) feed and pushes BUY / SELL alerts to a Telegram chat when a defined breakout/breakdown pattern occurs.

The bot does **not** place orders. It is alerts-only. The user reads the Telegram alert and decides whether to trade manually.

## 2. Scope

### In scope (v1)
- Polling Truth Social for new posts from a configured account.
- Hybrid bullish/bearish/neutral classification (keyword rules first, LLM fallback).
- Subscribing to IBKR live data for MCL via `ib_insync`.
- Detecting the spike → pullback → breakout (and inverse) pattern within a fixed observation window.
- Sending formatted Telegram alerts on detection.
- Structured logging and basic metrics (counts of posts seen, classified, alerts fired).
- Config via `.env` and a typed config object.
- Run as a long-lived process (CLI entrypoint).

### Out of scope (v1)
- Order placement (paper or live).
- Sources other than Truth Social.
- A web UI / dashboard.
- Backtesting.
- Multi-instrument support beyond MCL.

These are explicitly deferred to keep v1 small and shippable.

## 3. High-level architecture

The system has five logical components, designed to be independently testable:

1. **NewsListener** — polls Truth Social for new posts from one account, deduplicates, and emits `NewsEvent` objects.
2. **Classifier** — takes a `NewsEvent`, returns `Sentiment` (`BULLISH | BEARISH | NEUTRAL`) and a `confidence` float.
3. **PriceMonitor** — maintains a rolling buffer of MCL ticks and bars from IBKR.
4. **StrategyEngine** — for each non-neutral `NewsEvent`, runs the post-news observation window and decides whether the breakout/breakdown condition triggers an alert.
5. **Alerter** — formats and sends Telegram messages.

A thin **Orchestrator** wires the components together and owns the event loop.

The components communicate via in-process async queues; no external broker.

## 4. Components — detail

### 4.1 NewsListener (Truth Social)

**Responsibility:** produce a stream of new posts from one configured account.

**Approach:** scrape the public Truth Social profile page. Truth Social has no official public API. The implementation should:
- Use `httpx` (async) with a realistic User-Agent header.
- Poll on a configurable interval (default: every 30 seconds).
- Parse posts from the profile feed. The exact selector/JSON path will be discovered during implementation; the implementer should isolate the parsing logic behind a `_parse_feed(html_or_json) -> list[RawPost]` function so it can be updated when the site changes.
- Maintain a persisted set of seen post IDs (in a small SQLite file at `data/seen_posts.db`) so restarts don't re-emit old posts and don't re-fire alerts.
- Emit only posts newer than the bot's start time on first run (to avoid alert storms on launch).

**Interface:**
```
class NewsListener:
    async def stream() -> AsyncIterator[NewsEvent]
```

**`NewsEvent` fields:** `id: str`, `author: str`, `text: str`, `posted_at: datetime`, `received_at: datetime`, `url: str`.

**Failure handling:**
- HTTP errors: log, back off (exponential, capped at 5 min), keep polling.
- Parse errors (site structure changed): log a single ERROR with the raw payload truncated to 2 KB, send a one-time Telegram message saying "news listener degraded — manual check required", continue retrying.
- Never crash the bot on a parse error.

### 4.2 Classifier (hybrid)

**Responsibility:** label a `NewsEvent` as `BULLISH`, `BEARISH`, or `NEUTRAL` for crude oil.

**Stage 1 — keyword rules** (file: `classifier/keywords.yaml`):
- `bullish`: e.g. `sanctions`, `OPEC cut`, `production cut`, `attack on refinery`, `Iran strike`, `pipeline shutdown`, `escalation`, `embargo`.
- `bearish`: e.g. `ceasefire`, `release reserves`, `SPR release`, `production increase`, `OPEC raise`, `deal reached`, `tariff pause` (when context implies demand-positive… caveat: tariff signals are ambiguous and should fall through to stage 2).
- Implemented as case-insensitive substring matches over a normalized version of the post (strip URLs, collapse whitespace).
- If a post matches **only** bullish keywords with confidence ≥ threshold, label `BULLISH`. Mirror for bearish. If both or neither match, fall through.

**Stage 2 — LLM fallback:**
- Calls an LLM (provider configurable: `anthropic` or `openai`).
- Prompt: a short system message defining the task ("Classify this post for crude oil price impact in the next 30 minutes. Output exactly one of: BULLISH, BEARISH, NEUTRAL, with a confidence 0.0–1.0 and a one-sentence rationale. Be conservative — output NEUTRAL when uncertain.") and the post text.
- Output parsed as JSON; on parse failure, label `NEUTRAL`.
- Cache results by post ID so a restart doesn't re-bill.

**Output:**
```
@dataclass
class Classification:
    sentiment: Sentiment
    confidence: float
    rationale: str
    source: Literal["keywords", "llm", "cache"]
```

**Neutral handling:** the StrategyEngine ignores `NEUTRAL` events entirely. No alert, no observation window.

### 4.3 PriceMonitor (IBKR via ib_insync)

**Responsibility:** maintain a rolling, queryable view of MCL price data.

**Connection:** `ib_insync.IB().connectAsync(host, port=7497, clientId=...)`. Default to paper-trading port; production port (7496) configurable.

**Subscription:** `Future` contract for MCL, front month. Implementer should use `IB.reqContractDetails` to resolve the front-month contract dynamically on startup (don't hardcode the contract month).

**Data:**
- 5-second bars, kept for the last 30 minutes (rolling buffer in memory).
- Tick-level last price kept for the last 5 minutes.

**Interface:**
```
class PriceMonitor:
    async def start() -> None
    def get_window(end: datetime, lookback_seconds: int) -> PriceWindow
    @property
    def last_price() -> float | None
    @property
    def is_connected() -> bool
```

`PriceWindow` exposes `high`, `low`, `open`, `close`, and the underlying bar list.

**Failure handling:**
- TWS/Gateway disconnects: auto-reconnect with backoff. While disconnected, `is_connected` is False; the StrategyEngine refuses to run new windows and sends a Telegram message "price feed disconnected — alerts paused".
- Missing data subscription error: surface clearly on startup with a remediation hint ("Enable CL/MCL market data subscription in IBKR account").

### 4.4 StrategyEngine

**Responsibility:** given a non-neutral `NewsEvent`, run an observation window and decide whether to alert.

**State machine per event:**

For a `BULLISH` event at time `T`:
1. **Wait** until `T + 2 minutes` (configurable).
2. **Find spike high:** the maximum price in `[T, T + 2min]`. Call this `H`.
3. **Look for pullback:** monitor the next 5 minutes (configurable). A valid pullback is a close below `H − k × ATR_short` for some small `k` (default `k=0.5`) **or** a 3-bar lower-lows pattern. The pullback low is `L`.
4. **Look for breakout:** if price closes above `H` within `T + 10 min` (configurable total window), fire an alert.
5. **Timeout:** if no breakout by the end of the window, log "no setup", no alert.

`BEARISH` events use the inverse: dump low, bounce, breakdown below the dump low.

**Concurrency:** multiple events can be in flight simultaneously (each gets its own task). Cap concurrent in-flight events at 5 to avoid runaway memory.

**ATR_short:** average true range over the last 12 × 5-second bars (= 1 minute). If the buffer is too short, fall back to `k_static = 0.10` (i.e. require a $0.10 pullback on MCL). All thresholds live in `config.py`.

**Alert payload:** entry hint = breakout level, suggested stop = pullback extreme `L`, suggested target = entry + `1.5 × |entry − L|` (mirror for SELL).

### 4.5 Alerter (Telegram)

**Responsibility:** send formatted messages via Telegram bot API.

- Uses `httpx.AsyncClient`.
- Markdown V2 formatting.
- Retries on 5xx with exponential backoff, max 3 retries.
- Drops the message and logs ERROR if Telegram returns 4xx (likely a config problem; retrying won't help).
- Has three message types: `alert(payload)`, `system(text)` (for "feed disconnected" etc.), `startup(text)` (sent once on boot to confirm wiring).

**Alert format:**
```
🟢 BUY ALERT — MCL
Trigger: Trump post (BULLISH, 0.82)
"<truncated post text, 200 chars>"
Spike high: 78.42
Pullback low: 78.21
Breakout: 78.45 @ 14:07:32 UTC
Suggested stop: 78.21
Suggested target: 78.81 (1.5R)
```

## 5. Configuration

All runtime config in `.env`, loaded via `pydantic-settings`. A typed `Config` dataclass is the single source of truth — no `os.getenv` calls scattered through the code.

Required keys:
- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`
- `TRUTH_SOCIAL_HANDLE` (e.g. `realDonaldTrump`)
- `IBKR_HOST` (default `127.0.0.1`)
- `IBKR_PORT` (default `7497`)
- `IBKR_CLIENT_ID` (default `42`)
- `LLM_PROVIDER` (`anthropic` | `openai` | `none` to disable stage 2)
- `LLM_API_KEY` (required if provider is not `none`)
- `LOG_LEVEL` (default `INFO`)

Tunable strategy params (with defaults) live in `config/strategy.yaml`, not `.env`, since they're tweaked together:
- `news_wait_seconds: 120`
- `pullback_window_seconds: 300`
- `total_window_seconds: 600`
- `pullback_atr_multiplier: 0.5`
- `pullback_static_fallback: 0.10`
- `target_r_multiple: 1.5`

## 6. Persistence

A single SQLite file at `data/bot.db` with three tables:
- `seen_posts(id, author, posted_at, raw_text)` — dedupe.
- `classifications(post_id, sentiment, confidence, rationale, source, classified_at)` — cache + audit.
- `alerts(post_id, side, entry, stop, target, sent_at)` — audit trail.

No ORM needed; thin wrapper around `sqlite3` is sufficient.

## 7. Logging & observability

- Structured JSON logs to stdout (`structlog`).
- Key events: `news_received`, `classification`, `window_started`, `window_resolved` (alerted | timed_out), `alert_sent`, `feed_disconnected`, `feed_reconnected`.
- A `--status` CLI subcommand prints last 24h counts: posts seen, classified bullish/bearish/neutral, alerts sent.

## 8. CLI

A single entrypoint `python -m oilbot`:
- `python -m oilbot run` — runs the bot.
- `python -m oilbot test-telegram` — sends a test message and exits.
- `python -m oilbot test-ibkr` — connects, subscribes for 30 s, prints last prices, exits.
- `python -m oilbot classify "<text>"` — runs the classifier on a string and prints the result.
- `python -m oilbot status` — prints last 24h summary from SQLite.

## 9. Error handling philosophy

The bot is a long-lived process. **It must not crash on transient errors.** Each component catches its own exceptions, logs them, and either retries with backoff or degrades gracefully (and tells Telegram the user). The only exit conditions are:
- Misconfiguration on startup (missing keys, bad LLM provider name).
- An unrecoverable IBKR auth failure.
- SIGINT / SIGTERM (clean shutdown: drain in-flight tasks, close DB, disconnect IBKR).

## 10. Testing

- **Unit tests** for the keyword classifier and the StrategyEngine state machine, using a fake `PriceMonitor` that lets tests inject a sequence of bars.
- **Integration test** that boots the orchestrator with fake `NewsListener` and fake `PriceMonitor` and asserts a Telegram alert is produced for a scripted scenario.
- **Manual smoke tests** documented in `README.md` (test-telegram, test-ibkr commands above).
- No live-trading or live-news tests in CI.

## 11. Risk & legal notes

- This is alerts-only. The bot does not execute trades and does not provide investment advice.
- Truth Social scraping is in a legal grey area; respect `robots.txt` and rate-limit politely. If Truth Social changes terms or structure, the listener may need replacing.
- The user is responsible for IBKR data subscription costs and any trades they place based on alerts.
- Default to paper trading for any future order-execution work.

## 12. What "done" looks like for v1

- `python -m oilbot test-telegram` sends a message.
- `python -m oilbot test-ibkr` prints live MCL prices.
- `python -m oilbot classify "OPEC announces production cut"` returns BULLISH.
- `python -m oilbot run` boots, sends a startup message to Telegram, polls Truth Social, and survives a forced TWS disconnect/reconnect.
- The integration test passes: a fake bullish post + scripted price spike-pullback-breakout produces an alert.
