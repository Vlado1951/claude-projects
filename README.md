# oilbot

Watches Trump's Truth Social feed, classifies posts for crude oil price impact, and sends BUY/SELL alerts to Telegram when a spike → pullback → breakout pattern is detected on IBKR MCL (Micro Crude Oil). **Alerts only — no order placement.**

---

## How it works

1. **News listener** polls Truth Social every 30 s (configurable). New posts are classified as BULLISH, BEARISH, or NEUTRAL.
2. **Classifier** runs keyword matching first, falls back to an LLM (Anthropic or OpenAI) if keywords are ambiguous.
3. **Strategy engine** opens a 10-minute observation window on each non-neutral post:
   - Waits 2 min, finds the spike high (BULLISH) or dump low (BEARISH).
   - Watches 5 min for a pullback below the spike high by `k × ATR` (default `k = 0.5`; falls back to $0.10 if too few bars).
   - Watches the remaining time for a breakout above the spike high. On breakout, fires a Telegram alert with entry, stop, and target.
4. Up to 5 strategy windows run concurrently. Each result is recorded in a local SQLite database.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | Managed automatically by `uv` |
| [uv](https://docs.astral.sh/uv/) | `brew install uv` or `pip install uv` |
| TWS or IB Gateway | Paper port **4002** (Gateway) or **7497** (TWS). Must be running before `oilbot run`. |
| MCL market data subscription | Enable **US Futures / NYMEX** in IBKR Account Management → Market Data Subscriptions. |
| Telegram bot token + chat ID | Create a bot via [@BotFather](https://t.me/botfather); get your chat ID from [@userinfobot](https://t.me/userinfobot). |
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

# IBKR — defaults match IB Gateway paper port
IBKR_HOST=127.0.0.1
IBKR_PORT=4002          # 4002 = Gateway paper, 7497 = TWS paper, 7496 = TWS live
IBKR_CLIENT_ID=42

# Truth Social handle to watch
TRUTH_SOCIAL_HANDLE=realDonaldTrump

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

# 5. Strategy engine against live feed
uv run python -m oilbot dev-trigger BULLISH
# Expected: connects to IBKR, logs spike_high_found → pullback_found (or no_setup).
# Runs the full 10-minute window. A Telegram alert fires if a breakout occurs.

# 6. Database status
uv run python -m oilbot status
# Expected: last-24h summary with post, classification, and alert counts.
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
make test    # pytest (98 tests, no network required)
make install # uv sync --extra dev
```

### CLI reference

| Command | Description |
|---|---|
| `oilbot run` | Start the bot (news + IBKR + strategy) |
| `oilbot run --once-news` | Fetch one batch of posts and exit (smoke test) |
| `oilbot test-telegram` | Send a test Telegram message and exit |
| `oilbot test-ibkr` | Connect, stream MCL prices for 30 s, exit |
| `oilbot classify "<text>"` | Classify a post text and print result |
| `oilbot dev-trigger BULLISH\|BEARISH` | Inject a fake event into the strategy engine against the live price feed |
| `oilbot status` | Print last-24h post / classification / alert counts |

---

## Disclaimer

All alerts are informational only. The author accepts no responsibility for trading decisions made based on this software. IBKR data subscription costs and any trades placed are entirely the user's responsibility.
