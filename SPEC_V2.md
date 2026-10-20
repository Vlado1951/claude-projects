# Oil News Trading Alert Bot — v2 Specification

This document extends `SPEC.md` (v1). It adds new signal sources and one new architectural concept (price confirmation), all using free public endpoints. **Read v1 first.** Anything not changed here behaves exactly as in v1.

## 1. Goals

Add additional free oil-relevant signal sources beyond Trump's Truth Social, so the bot reacts to a broader set of market-moving events. All sources must be free — no paid subscriptions, no paid APIs, no rate-limited tiers that require a credit card.

Specifically:
- More news sources (OPEC, Google News oil/energy, hurricane forecasts).
- Scheduled data releases (EIA inventories, Baker Hughes rigs).
- Macroeconomic calendar events (FOMC, NFP).
- Optional price-based confirmation gating (Brent, DXY, energy ETFs) before firing an alert.

The classifier and strategy engine from v1 remain unchanged.

## 2. New signal sources — overview

| # | Source | Type | Frequency | Endpoint |
|---|--------|------|-----------|----------|
| 1 | EIA Weekly Petroleum Status | Scheduled, structured | Weekly Wed 10:30 ET | api.eia.gov (free key) |
| 2 | EIA Baker Hughes Rig Count | Scheduled, structured | Weekly Fri 13:00 ET | api.eia.gov (free key) |
| 3 | OPEC press releases | News, RSS | Irregular | opec.org RSS |
| 4 | Google News oil/energy | News, RSS | Continuous | news.google.com RSS (free, no key) |
| 5 | NOAA National Hurricane Center | News, RSS | Continuous (active hurricane season) | nhc.noaa.gov RSS |
| 6 | FOMC + NFP economic calendar | Scheduled, calendar | ~8/year + monthly | federalreserve.gov + BLS |
| 7 | Price confirmation: Brent, DXY, XLE | Real-time price | Continuous | yfinance (free) |

Sources 1–6 emit `NewsEvent` (or a structured subclass) into the same pipeline as Truth Social. Source 7 is a separate `PriceContext` abstraction queried by the strategy engine before firing alerts.

## 3. Source details

### 3.1 EIA Weekly Petroleum Status Report

The single biggest scheduled mover of US crude prices. Released every Wednesday at 10:30 ET (10:00 in advance week of holidays).

**Endpoint:** `https://api.eia.gov/v2/petroleum/stoc/wstk/data/` with a free API key from `https://www.eia.gov/opendata/register.php`.

**What we extract:**
- Crude oil stocks change (week-over-week, in millions of barrels).
- Gasoline stocks change.
- Distillate stocks change.
- Refinery utilization rate.

**Listener behavior:**
- Schedules a poll every Wednesday at 10:30 ET (with a 5-minute pre-window check at 10:25 to be ready, and re-polls every 30 seconds until new data appears, capped at 30 minutes).
- Compares the latest weekly value to the previous week's value to compute the delta.
- Emits a structured `EIAReleaseEvent` with the deltas.

**Classification:**
- A *crude draw* (stocks fell) larger than ~2 million barrels = `BULLISH`.
- A *crude build* (stocks rose) larger than ~2 million barrels = `BEARISH`.
- Smaller magnitudes = `NEUTRAL` (don't fire).
- Thresholds in `config/strategy.yaml` so they're tunable.

The hybrid classifier from v1 gets a structured-event branch that bypasses the keyword/LLM stages for these.

### 3.2 EIA Baker Hughes Rig Count

Same API, different dataset. Released every Friday at 13:00 ET. Lower-impact than weekly inventories but easy to add since the integration code is already there.

**What we extract:** Total US oil rig count, week-over-week delta.

**Classification:**
- Rigs falling ≥ 5 week-over-week = mild `BULLISH` (fewer rigs = less future supply).
- Rigs rising ≥ 5 = mild `BEARISH`.
- Otherwise `NEUTRAL`.

Same `EIAReleaseEvent` shape as 3.1.

### 3.3 OPEC press releases

OPEC announcements move oil more than almost any other source — production cuts/raises, meeting decisions, surprise statements.

**Endpoint:** `https://www.opec.org/opec_web/en/press_room/press_release.xml` (RSS).

**Listener behavior:** Generic RSS poller (see §4.2), poll every 5 minutes during OPEC meeting windows (configurable schedule), every 30 minutes otherwise.

**Classification:** Standard text classification (keyword + LLM if enabled). The keyword list gains OPEC-specific terms: `production cut`, `production increase`, `voluntary cut`, `output target`, `compliance`.

### 3.4 Google News oil/energy

Google News RSS search — free, no API key, no rate limit documented. Catches geopolitical events Trump won't post about. Reuters shut down their public RSS feeds in 2020; Google News aggregates the same coverage (Reuters, AP, Bloomberg, FT, etc.) through a single public endpoint.

**Endpoint:** `https://news.google.com/rss/search?q=oil+OR+crude+OR+OPEC+OR+sanctions+when:1d&hl=en-US&gl=US&ceid=US:en`

The `when:1d` query operator restricts results to the last 24 hours, keeping the feed fresh without dedup noise from old articles cycling back in.

**Source name:** `google_news_oil`

**Listener behavior:** Generic RSS poller, every 60 seconds. Keyword pre-filter (case-insensitive) before emitting: `crude`, `oil`, `OPEC`, `Iran`, `Russia`, `pipeline`, `refinery`, `sanctions`, `embargo`, `Strait of Hormuz`, `Houthi`. Posts that don't match are dropped silently — keeps classification load low.

### 3.5 NOAA National Hurricane Center

The Gulf of Mexico produces ~15% of US crude. Hurricane forecasts move oil during the June–November season.

**Endpoint:** `https://www.nhc.noaa.gov/index-at.xml` (Atlantic) and `https://www.nhc.noaa.gov/index-ep.xml` (Eastern Pacific — less relevant for US oil but cheap to include).

**Listener behavior:** Generic RSS poller, every 5 minutes during hurricane season, paused outside the season (December–May). Keyword pre-filter for `Gulf`, `Texas`, `Louisiana`, `Mexico` to focus on production-relevant storms.

**Classification:** All emitted events default to `BULLISH` regardless of classifier output, because storm threats to Gulf production are universally bullish for oil. Exception: storms weakening or dissipating are `BEARISH` (caught by classifier with terms like `downgraded`, `dissipated`).

### 3.6 FOMC + NFP economic calendar

These events move the US dollar, which inversely correlates with oil. Predictable timing — calendar, not RSS.

**Sources:**
- FOMC meeting dates: scraped/parsed from `https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm`. Updates rarely; cache the parsed list and refresh weekly.
- NFP release dates: first Friday of each month at 8:30 ET (deterministic — no API needed, compute from calendar).

**Listener behavior:** A `CalendarListener` with no polling. It emits a "watch" event 5 minutes before each scheduled release and a "released" event 30 seconds after, both labeled `NEUTRAL` (the bot doesn't try to predict the direction). Their value is in being on the price feed and ready — the strategy will catch any genuine spike pattern.

This is a soft signal: the strategy engine doesn't auto-alert on these; it just makes the bot aware that a known volatility window is happening.

### 3.7 Price confirmation signals

A new abstraction. Before firing an alert, optionally verify that correlated assets are moving in the same direction. Reduces false positives.

**Source:** Yahoo Finance via `yfinance` (free, no key, ~15 min delayed for futures, ~real-time for equities and ETFs).

**Tickers tracked:**
- `BZ=F` — Brent crude. Should move with WTI.
- `CL=F` — WTI front month. Cross-check with IBKR.
- `DX-Y.NYB` — US Dollar Index. Should move *opposite* WTI.
- `XLE` — Energy Select Sector SPDR ETF. Should move with WTI.
- `XOP` — S&P Oil & Gas Exploration ETF. Same as XLE.

**Behavior:** A `PriceContext` class polls these every 60 seconds and exposes:
```
PriceContext.is_aligned(side: Literal["BUY", "SELL"]) -> ConfirmationResult
```

`ConfirmationResult` returns `aligned`, `mixed`, or `disaligned`, plus the per-ticker direction observed.

**Strategy gate (configurable):**
- `CONFIRMATION_MODE=off` — ignore confirmation entirely (v1 behavior).
- `CONFIRMATION_MODE=advisory` — fire the alert regardless, but include the confirmation result in the Telegram message.
- `CONFIRMATION_MODE=required` — only fire if `aligned`; downgrade to `system` Telegram message ("setup detected but unconfirmed") otherwise.

Default: `advisory`. It's informative without being trigger-happy.

## 4. Architecture additions

### 4.1 `NewsSource` Protocol (formalize the v1 implicit pattern)

Every listener implements:
```python
class NewsSource(Protocol):
    name: str  # for logging and event provenance
    async def stream() -> AsyncIterator[NewsEvent]: ...
```

`TruthSocialListener` from v1 already conforms; just add the `name` attribute. Classes 4.2 / 4.3 / 4.4 below also conform.

### 4.2 `GenericRSSListener`

One implementation, multiple instances configured per-source. Constructor takes:
- `name`
- `feed_url`
- `poll_interval_seconds`
- `keyword_filter: list[str] | None` (optional pre-classifier filter)
- `default_sentiment: Sentiment | None` (e.g. NOAA defaults `BULLISH`)
- `active_window: Callable[[datetime], bool] | None` (e.g. hurricane season gate)

Used for OPEC, Reuters, NOAA, and any future RSS feed. Single piece of code, configured differently.

### 4.3 `EIAReleaseListener`

Subclass of `NewsSource`. Holds a schedule (cron-like: weekday + time + dataset path) and an EIA API client. Emits `EIAReleaseEvent` (a `NewsEvent` subclass with extra `metric_delta: float`).

### 4.4 `CalendarListener`

Subclass of `NewsSource`. Holds a static or dynamically-fetched list of `(datetime, label)` pairs. Emits "watch" and "released" events at the appropriate times.

### 4.5 `PriceContext`

New top-level component. Constructed by the orchestrator with a list of yfinance tickers. Maintains a small in-memory cache of recent prices and 1-minute returns per ticker.

The `StrategyEngine` gets an optional `PriceContext` injected. Where v1 always called `Alerter.alert()` on a setup completion, v2 first calls `price_context.is_aligned(side)` and behaves per `CONFIRMATION_MODE`.

### 4.6 Classifier — structured-event branch

`HybridClassifier.classify(event)` becomes:
```python
if isinstance(event, EIAReleaseEvent):
    return self._classify_eia(event)
# otherwise text path (v1 keywords + LLM)
```

`_classify_eia` is deterministic — pure thresholds from config, no LLM call.

### 4.7 Configuration additions

New keys in `.env`:
```
EIA_API_KEY=<free key from eia.gov/opendata/register.php>
CONFIRMATION_MODE=advisory  # off | advisory | required
NEWS_SOURCES=truth_social,opec_rss,google_news_oil,nhc_atlantic,eia_petroleum,eia_rigs,fomc,nfp
```

`NEWS_SOURCES` is a comma-separated allow-list. Disabling a source is just removing it from this list — no code change.

New keys in `config/strategy.yaml`:
```
eia_crude_draw_threshold_mbbl: 2.0
eia_crude_build_threshold_mbbl: 2.0
eia_rig_change_threshold: 5
confirmation_mode: advisory  # mirrors env, env wins
```

### 4.8 Persistence additions

`seen_posts` table gets a `source` column (`truth_social`, `opec_rss`, etc.) so the per-source dedupe is independent and one source breaking doesn't lose state for others.

`alerts` table gets a `confirmation_status` column (`aligned`, `mixed`, `disaligned`, `not_checked`) for post-hoc audit.

Existing rows backfilled with `source='truth_social'` and `confirmation_status='not_checked'` via a one-time migration on startup if columns are missing.

### 4.9 Telegram alert format addition

Append to the v1 alert payload, when `CONFIRMATION_MODE != off`:
```
Confirmation: aligned ✅ (Brent +0.4%, DXY -0.2%, XLE +0.6%)
```
or
```
Confirmation: mixed ⚠️ (Brent +0.4%, DXY +0.1%, XLE -0.1%)
```

## 5. What's deliberately not in v2

- **X (Twitter) API.** Costs $100+/month even at the lowest paid tier. Out of scope for free-only.
- **Bloomberg feeds.** Paid.
- **API (American Petroleum Institute) inventory.** Real-time data is paid; the headline numbers leak into Reuters within minutes anyway, so the Reuters listener captures them indirectly.
- **China PMI direct API.** Free direct sources are unreliable; covered indirectly via the Reuters listener with keywords `China PMI`, `manufacturing PMI`.
- **Backtesting.** Still v3 territory.
- **Multi-instrument.** Still MCL only.

## 6. Risk & operational notes

- **EIA API key** is free and unlimited but should still be kept out of git. Same `.env` discipline as v1.
- **Reuters RSS rate limits** are not strictly published. Polling at 60s should be safe; if you see 429 responses, back off. The generic RSS listener should handle this.
- **NOAA RSS** is reliable but pause polling outside hurricane season — saves bandwidth and noise.
- **Yahoo Finance** is not officially supported as an API. Expect occasional `yfinance` library breakage when Yahoo changes their internals. Pin the version and update on a rhythm.
- **Confirmation gating in `required` mode** will reduce alert frequency. Run in `advisory` mode for at least a week to gather a baseline before flipping to `required`.

## 7. What "done" looks like for v2

- All 7 new sources can be individually enabled/disabled via `NEWS_SOURCES`.
- `python -m oilbot test-eia` fetches the latest EIA weekly petroleum data and prints the deltas.
- `python -m oilbot test-rss <name>` polls a configured RSS source once and prints what it would emit.
- `python -m oilbot test-confirmation` prints current alignment for `BUY` and `SELL` against live yfinance data.
- A new integration test scripts an EIA release event with a 5-million-barrel draw and asserts a `BULLISH` alert fires through the full pipeline.
- README updated with the new sources, their data, and the `CONFIRMATION_MODE` knob.
- All tests still green; ruff and mypy clean.
