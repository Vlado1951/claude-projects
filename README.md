# oilbot

Watches multiple oil-relevant news sources, classifies events for crude oil price impact, and sends BUY/SELL alerts to Telegram when a spike → pullback → breakout pattern is detected on IBKR MCL (Micro Crude Oil). **Alerts only — no order placement.**

---

## How it works

1. **News sources** emit events (Truth Social posts, RSS headlines, EIA data releases, FOMC/NFP calendar events). Each source is independently enabled via `NEWS_SOURCES`.
2. **Classifier** labels each event BULLISH, BEARISH, or NEUTRAL. Keyword matching runs first; LLM fallback (Anthropic / OpenAI) handles ambiguous posts. EIA releases are classified directly from the magnitude of the inventory change.
3. **Strategy engine** opens a 10-minute observation window on each non-neutral event:
   - Waits 2 min, finds the spike high (BULLISH) or dump low (BEARISH).
   - Watches 5 min for a pullback below the spike high by `k × ATR` (default `k = 0.5`; falls back to $0.10 if too few bars).
   - Watches the remaining time for a breakout above the spike high. On breakout, fires a Telegram alert with entry, stop, and target.
4. **Price confirmation** (optional) cross-checks Brent, WTI, DXY, XLE, and XOP direction before firing. Controlled by `CONFIRMATION_MODE`.
5. Up to 5 strategy windows run concurrently. All events and alerts are recorded in a local SQLite database.

---

## News sources

Enable sources with a comma-separated `NEWS_SOURCES` value in `.env`. Each source is independent — mix and match as needed.

| Source key | What it monitors | Schedule | Extra requirement |
|---|---|---|---|
| `truth_social` | Trump's Truth Social posts | Every 30 s (configurable) | None |
| `opec_rss` | OPEC press releases | Every 5 min | None |
| `google_news_oil` | Oil/energy headlines (keyword-filtered) | Every 60 s | None |
| `nhc_atlantic` | NHC Atlantic hurricane advisories | Every 5 min (Jun–Nov only) | None |
| `eia_petroleum` | Weekly crude oil inventory (EIA) | Wed 10:30 ET release window | `EIA_API_KEY` |
| `eia_rigs` | Monthly oil rig count (EIA) | Fri 13:00 ET release window | `EIA_API_KEY` |
| `calendar` | FOMC meeting dates, NFP release dates | Event-driven | None |

**Example** — run all sources:
```ini
NEWS_SOURCES=truth_social,opec_rss,google_news_oil,nhc_atlantic,eia_petroleum,eia_rigs,calendar
```

### EIA API key

The EIA sources require a free API key (no credit card, no payment):

1. Register at <https://www.eia.gov/opendata/register.php>
2. Copy the key into `.env` as `EIA_API_KEY=<your-key>`

The bot polls 5 minutes before each scheduled release and checks for new data every 60 seconds for up to 30 minutes.

### Calendar source

Fetches FOMC meeting dates directly from the Federal Reserve website and computes NFP (Non-Farm Payroll) dates as the first Friday of each month. Emits a `watch` event 5 minutes before each release and a `released` event when the data drops. No trading signal is generated — the alert is informational so you know a market-moving event is imminent.

---

## Price confirmation

`CONFIRMATION_MODE` controls whether direction agreement across Brent, WTI, DXY, XLE, and XOP is required before firing an alert.

| Mode | Behaviour |
|---|---|
| `off` | No confirmation check. Alert fires on breakout regardless. |
| `advisory` *(default)* | Confirmation is checked and appended to the alert, but never suppresses it. |
| `required` | A `disaligned` result (≤1 of 5 tickers moving the right way) suppresses the alert entirely. |

Alignment thresholds: ≥ 4 tickers aligned → `aligned`; 2–3 → `mixed`; ≤ 1 → `disaligned`.

Data is fetched from Yahoo Finance every 60 seconds in a background loop. Run `make test-confirmation` to see the current alignment snapshot.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | Managed automatically by `uv` |
| [uv](https://docs.astral.sh/uv/) | `brew install uv` or `pip install uv` |
| TWS or IB Gateway | Paper port **4002** (Gateway) or **7497** (TWS). Must be running before `oilbot run`. |
| MCL market data subscription | Enable **US Futures / NYMEX** in IBKR Account Management → Market Data Subscriptions. |
| Telegram bot token + chat ID | Create a bot via [@BotFather](https://t.me/botfather); get your chat ID from [@userinfobot](https://t.me/userinfobot). |
| EIA API key *(EIA sources only)* | Free at <https://www.eia.gov/opendata/register.php> |
| LLM API key *(optional)* | Anthropic or OpenAI. Set `LLM_PROVIDER=none` to skip (keyword-only classification). |

---

## Setup

```bash
# 1. Clone
git clone <repo-url> oilbot && cd oilbot

# 2. Install dependencies (uv picks the right Python automatically)
make install          # or: uv sync --extra dev

# 3. Configure
cp .env.example .env
$EDITOR .env          # fill in TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, and optionally LLM_*
```

### `.env` reference

```ini
# Required
TELEGRAM_TOKEN=<bot-token>
TELEGRAM_CHAT_ID=<your-chat-id>

# Truth Social handle to watch
TRUTH_SOCIAL_HANDLE=realDonaldTrump

# IBKR — defaults match IB Gateway paper port
IBKR_HOST=127.0.0.1
IBKR_PORT=4002          # 4002 = Gateway paper, 7497 = TWS paper, 7496 = TWS live
IBKR_CLIENT_ID=42

# News polling interval (seconds — Truth Social only)
NEWS_POLL_INTERVAL_SECONDS=30

# Comma-separated list of enabled news sources
# Available: truth_social, opec_rss, google_news_oil, nhc_atlantic,
#            eia_petroleum, eia_rigs, calendar
NEWS_SOURCES=truth_social

# EIA API (free key from https://www.eia.gov/opendata/register.php)
EIA_API_KEY=

# EIA classification thresholds (optional — defaults shown)
EIA_CRUDE_THRESHOLD_MBBL=2.0   # draw/build magnitude in million barrels to classify
EIA_RIG_THRESHOLD=5            # rig count change to classify

# Price confirmation: off | advisory (default) | required
CONFIRMATION_MODE=advisory

# Classifier — set provider + key to enable LLM fallback
LLM_PROVIDER=none       # none | anthropic | openai
LLM_API_KEY=

# Strategy thresholds (all optional — defaults shown)
SPIKE_WINDOW_SECONDS=120
PULLBACK_WINDOW_SECONDS=300
TOTAL_WINDOW_SECONDS=600
PULLBACK_K=0.5
K_STATIC=0.10
TARGET_RR=1.5

LOG_LEVEL=INFO

# Forward-test / outcome tracking
OUTCOME_TIMEOUT_MINUTES=60   # close unfilled or lingering alerts after this many minutes
FILL_MODEL=next_tick         # next_tick | theoretical
STOP_SLIPPAGE_TICKS=0        # extra adverse ticks added to stop_hit exit price
TICK_SIZE=0.01               # contract tick size (MCL = 0.01)
OUTCOME_TELEGRAM=false       # send outcome resolution messages via Telegram
```

---

## Forward-test mode

Every time the bot fires an alert it writes a row to the `alert_outcomes` table in the
database. The **outcome tracker** then watches the live MCL price stream and resolves
each row to one of three terminal states:

| Status | Meaning |
|---|---|
| `target_hit` | Price reached the alert target |
| `stop_hit` | Price reached the alert stop (adverse) |
| `timeout` | Neither level hit within `OUTCOME_TIMEOUT_MINUTES` |

### R-multiples

All profit/loss is expressed in **R-multiples**: units of initial risk.
`R = (exit − entry) / |entry − stop|`

- `+1.5 R` means the trade made 1.5× the amount risked.
- `−1.0 R` means the trade lost exactly the amount risked (a standard stop-out).

**Profit factor** = sum of winning Rs ÷ |sum of losing Rs|.  A profit factor above 1.0
means the strategy is net-profitable at the assumed fill model.

### MFE and MAE

- **MFE** (Maximum Favorable Excursion) — the best price seen after fill, in R units.  
  High MFE relative to `TARGET_RR` suggests the target is set too conservatively.
- **MAE** (Maximum Adverse Excursion) — the worst price seen after fill, in R units.  
  Low MAE (close to −1 R) on winning trades means the position was threatened before recovering.

### Fill model

- `next_tick` (default) — fill is recorded on the first live tick that crosses the entry
  price.  Realistic but requires the bot to be running when the price moves.
- `theoretical` — fill is assumed immediately at the exact entry price.  Useful for
  back-of-envelope estimates; always gives the best possible entry.

### Viewing results

```bash
# Text summary (all time)
uv run python -m oilbot performance

# Last 7 days, broken down by sentiment
uv run python -m oilbot performance --since 7d --by sentiment

# Self-contained HTML report
uv run python -m oilbot performance --html report.html

# Makefile shortcut (writes report.html in the project root)
make performance
```

---

## Smoke-test checklist

Run these in order before leaving the bot running unattended.

```bash
# 1. Telegram wiring
uv run python -m oilbot test-telegram
# Expected: "oilbot test message" arrives in your Telegram chat.

# 2. IBKR connection and live data
uv run python -m oilbot test-ibkr
# Expected: front-month MCL contract printed, ≥ 5 price updates over 30 s.

# 3. Keyword classifier
uv run python -m oilbot classify "OPEC announces production cut"
# Expected: BULLISH (source: keywords)

uv run python -m oilbot classify "Ceasefire reached in Middle East"
# Expected: BEARISH (source: keywords)

uv run python -m oilbot classify "Good morning everyone"
# Expected: NEUTRAL

# 4. News feed (fetches latest posts, no DB write)
uv run python -m oilbot run --once-news
# Expected: up to 5 recent Truth Social posts printed.

# 5. RSS sources — test each feed independently (no IBKR required)
uv run python -m oilbot test-rss opec_rss
# Expected: recent OPEC press releases printed, or "no entries" if feed is quiet.

uv run python -m oilbot test-rss google_news_oil
# Expected: up to 5 oil/energy headlines from the last 24 h.

uv run python -m oilbot test-rss nhc_atlantic
# Expected: NHC Atlantic advisory entries (quiet outside Jun–Nov hurricane season).

# 6. EIA data (requires EIA_API_KEY in .env)
uv run python -m oilbot test-eia
# Expected: latest weekly crude stocks printed with BULLISH/BEARISH/NEUTRAL classification.

uv run python -m oilbot test-eia eia_rigs
# Expected: latest monthly rig count printed with classification.

# 7. Economic calendar
uv run python -m oilbot test-calendar
# Expected: next 5 FOMC meeting dates and next 5 NFP dates printed.

# 8. Price confirmation
uv run python -m oilbot test-confirmation
# Expected: BUY/SELL alignment printed for Brent, WTI, DXY, XLE, XOP.
# Tickers with data older than 15 min are marked (stale).

# 9. Strategy engine against live feed
uv run python -m oilbot dev-trigger BULLISH
# Expected: connects to IBKR, logs spike_high_found → pullback_found (or no_setup).
# Runs the full 10-minute window. A Telegram alert fires if a breakout occurs.

# 10. Database status
uv run python -m oilbot status
# Expected: last-24h summary with post, classification, and alert counts,
# plus a per-source breakdown once multiple sources are active.
```

---

## Running the bot

```bash
make run
# or: uv run python -m oilbot run
```

The bot sends a startup Telegram message on boot, then runs until interrupted.
Stop it cleanly with **Ctrl-C** — in-flight strategy windows are drained (up to 30 s) before exit.

---

## Running as a service

### systemd (Linux)

Create `/etc/systemd/system/oilbot.service`:

```ini
[Unit]
Description=oilbot — oil news trading alert bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=<your-user>
WorkingDirectory=/home/<your-user>/oilbot
EnvironmentFile=/home/<your-user>/oilbot/.env
ExecStart=/home/<your-user>/oilbot/.venv/bin/python -m oilbot run
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now oilbot
sudo journalctl -fu oilbot
```

### pm2 (macOS / any Node environment)

```bash
pm2 start "uv run python -m oilbot run" \
  --name oilbot \
  --cwd /path/to/oilbot \
  --restart-delay 10000
pm2 save
pm2 logs oilbot
```

---

## Development

```bash
make lint    # ruff + mypy
make test    # pytest (191 tests, no network required)
make install # uv sync --extra dev
```

### CLI reference

| Command | Description |
|---|---|
| `oilbot run` | Start the bot (all enabled sources + IBKR + strategy) |
| `oilbot run --once-news` | Fetch one batch of posts and exit (smoke test) |
| `oilbot test-telegram` | Send a test Telegram message and exit |
| `oilbot test-ibkr` | Connect, stream MCL prices for 30 s, exit |
| `oilbot test-rss [SOURCE]` | Fetch one RSS poll and print emitted events |
| `oilbot test-eia [SOURCE]` | Fetch latest EIA release and print classification (`eia_petroleum` default) |
| `oilbot test-calendar` | Print next 5 FOMC and NFP dates |
| `oilbot test-confirmation` | Fetch live prices and print BUY/SELL alignment |
| `oilbot classify "<text>"` | Classify a post text and print result |
| `oilbot dev-trigger BULLISH\|BEARISH` | Inject a fake event into the strategy engine against the live price feed |
| `oilbot status` | Print last-24h post / classification / alert counts |
| `oilbot performance [--since 7d] [--by sentiment] [--html PATH]` | Show forward-test outcome statistics |

---

## Disclaimer

All alerts are informational only. The author accepts no responsibility for trading decisions made based on this software. IBKR data subscription costs and any trades placed are entirely the user's responsibility.
