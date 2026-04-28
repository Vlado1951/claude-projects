# Oil News Trading Alert Bot

Watches Trump's Truth Social feed, classifies posts for crude oil impact, and sends BUY/SELL alerts to Telegram when a spike→pullback→breakout pattern is detected on IBKR MCL. Alerts only — no order placement.

## Setup

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) installed
- TWS or IB Gateway running (paper port 7497 by default)
- A Telegram bot token and chat ID
- An LLM API key (Anthropic or OpenAI) if using the LLM fallback classifier

### Install

```bash
uv sync
```

### Configure

```bash
cp .env.example .env
# edit .env and fill in the required values
```

### Run

```bash
# Verify Telegram wiring
python -m oilbot test-telegram

# Verify IBKR connection and data feed
python -m oilbot test-ibkr

# Classify a post manually
python -m oilbot classify "OPEC announces production cut"

# Check last 24h summary
python -m oilbot status

# Run the bot
python -m oilbot run
```

## IBKR data subscription

MCL (Micro Crude Oil) requires an active market data subscription in your IBKR account. If `test-ibkr` reports a data permission error, enable the "US Futures" or "NYMEX" data bundle in Account Management → Market Data Subscriptions.

## Notes

- The bot defaults to the paper-trading port (7497). Set `IBKR_PORT=7496` for live.
- Strategy parameters are in `config/strategy.yaml` and can be tuned without restarting if you edit and restart the process.
- All alerts are informational only. The user decides whether to trade.
