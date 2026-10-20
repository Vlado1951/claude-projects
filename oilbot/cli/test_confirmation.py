from __future__ import annotations

from oilbot.config import Config
from oilbot.price.context import _TICKER_LABELS, PriceContext


async def test_confirmation(config: Config) -> None:
    print("Fetching prices from Yahoo Finance…")
    ctx = PriceContext()
    await ctx.fetch_once()

    for side in ("BUY", "SELL"):
        result = ctx.is_aligned(side)
        parts = []
        for sym, arrow in result.details.items():
            label = _TICKER_LABELS.get(sym, sym)
            part = f"{label}{arrow}"
            if sym in result.stale_symbols:
                part += " (stale)"
            parts.append(part)
        detail_str = ", ".join(parts) if parts else "no data"
        print(f"{side}: {result.status} ({detail_str})")
