from __future__ import annotations

from oilbot.config import Config
from oilbot.price.context import _TICKER_LABELS, PriceContext


async def test_confirmation(config: Config) -> None:
    print("Fetching prices from Yahoo Finance…")
    ctx = PriceContext()
    await ctx.fetch_once()

    for side in ("BUY", "SELL"):
        result = ctx.is_aligned(side)
        parts = [
            f"{_TICKER_LABELS.get(sym, sym)}{arrow}"
            for sym, arrow in result.details.items()
        ]
        detail_str = ", ".join(parts) if parts else "no data"
        print(f"{side}: {result.status} ({detail_str})")
