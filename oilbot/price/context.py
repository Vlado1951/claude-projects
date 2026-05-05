from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

import structlog

log = structlog.get_logger()

_POLL_INTERVAL = 60.0
_WINDOW_SECS = 5 * 60  # 5-minute lookback
_DIRECTION_THRESHOLD = 0.0001  # ~0.01% move to register a direction

# Expected direction per ticker for a BULLISH/BUY signal.
# DXY moves inverse to oil, so it should fall when oil rises.
_BULL_EXPECTED: dict[str, Literal["up", "down"]] = {
    "BZ=F":     "up",
    "CL=F":     "up",
    "DX-Y.NYB": "down",
    "XLE":      "up",
    "XOP":      "up",
}

_TICKER_LABELS = {
    "BZ=F":     "Brent",
    "CL=F":     "WTI",
    "DX-Y.NYB": "DXY",
    "XLE":      "XLE",
    "XOP":      "XOP",
}


@dataclass
class ConfirmationResult:
    status: Literal["aligned", "mixed", "disaligned"]
    aligned_count: int
    total_checked: int
    details: dict[str, str] = field(default_factory=dict)  # symbol → "↑" / "↓" / "?"

    def summary(self) -> str:
        parts = [
            f"{_TICKER_LABELS.get(sym, sym)}{arrow}"
            for sym, arrow in self.details.items()
        ]
        return f"{self.status} ({', '.join(parts)})"


class PriceContext:
    """Polls Yahoo Finance every 60 s and answers alignment queries."""

    TICKERS = list(_BULL_EXPECTED.keys())

    def __init__(self) -> None:
        # deque of (timestamp, price) per ticker — pruned to _WINDOW_SECS
        self._windows: dict[str, deque[tuple[datetime, float]]] = {
            t: deque() for t in self.TICKERS
        }

    # ── public API ────────────────────────────────────────────────────────────

    def is_aligned(self, side: Literal["BUY", "SELL"]) -> ConfirmationResult:
        aligned = 0
        details: dict[str, str] = {}
        for symbol in self.TICKERS:
            direction = self._direction(symbol)
            expected = _BULL_EXPECTED[symbol] if side == "BUY" else (
                "down" if _BULL_EXPECTED[symbol] == "up" else "up"
            )
            if direction == "unknown":
                details[symbol] = "?"
            else:
                arrow = "↑" if direction == "up" else "↓"
                details[symbol] = arrow
                if direction == expected:
                    aligned += 1

        known = sum(1 for v in details.values() if v != "?")
        if aligned >= 4:
            status: Literal["aligned", "mixed", "disaligned"] = "aligned"
        elif aligned >= 2:
            status = "mixed"
        else:
            status = "disaligned"

        return ConfirmationResult(
            status=status,
            aligned_count=aligned,
            total_checked=known,
            details=details,
        )

    async def fetch_once(self) -> None:
        """Fetch current prices for all tickers (one-shot, for tests and CLI)."""
        await asyncio.get_running_loop().run_in_executor(None, self._sync_fetch)

    async def start(self) -> None:
        """Background polling loop — runs until cancelled."""
        while True:
            try:
                await self.fetch_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("price_context_fetch_error", error=str(exc))
            await asyncio.sleep(_POLL_INTERVAL)

    # ── internals ─────────────────────────────────────────────────────────────

    def _sync_fetch(self) -> None:
        import yfinance as yf

        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=_WINDOW_SECS)

        # Batch download: one HTTP call for all tickers
        raw = yf.download(
            self.TICKERS,
            period="1d",
            interval="1m",
            progress=False,
            auto_adjust=True,
            multi_level_index=False,
        )
        if raw.empty:
            log.warning("price_context_empty_response")
            return

        close = raw.get("Close") if "Close" in raw.columns else raw
        for symbol in self.TICKERS:
            try:
                series = close[symbol].dropna() if hasattr(close, "columns") else close.dropna()
                if series.empty:
                    continue
                # Populate the window from recent historical bars so _direction()
                # can compute a direction immediately on the first fetch.
                dq = self._windows[symbol]
                dq.clear()
                for ts_idx, price_val in series.items():
                    ts = ts_idx.to_pydatetime()
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=UTC)
                    else:
                        ts = ts.astimezone(UTC)
                    if ts >= cutoff:
                        dq.append((ts, float(price_val)))
                if dq:
                    log.debug(
                        "price_context_updated",
                        symbol=symbol,
                        price=round(dq[-1][1], 4),
                        points=len(dq),
                    )
            except Exception as exc:
                log.warning("price_context_symbol_error", symbol=symbol, error=str(exc))

    def _direction(self, symbol: str) -> Literal["up", "down", "flat", "unknown"]:
        dq = self._windows[symbol]
        if len(dq) < 2:
            return "unknown"
        oldest_price = dq[0][1]
        latest_price = dq[-1][1]
        if oldest_price == 0:
            return "unknown"
        change = (latest_price - oldest_price) / oldest_price
        if change > _DIRECTION_THRESHOLD:
            return "up"
        if change < -_DIRECTION_THRESHOLD:
            return "down"
        return "flat"

    def inject(self, symbol: str, price: float, ts: datetime | None = None) -> None:
        """Test helper: inject a price reading directly."""
        now = ts or datetime.now(UTC)
        cutoff = now - timedelta(seconds=_WINDOW_SECS)
        dq = self._windows[symbol]
        dq.append((now, price))
        while dq and dq[0][0] < cutoff:
            dq.popleft()
