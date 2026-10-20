from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import structlog

log = structlog.get_logger()

_POLL_INTERVAL = 60.0
_WINDOW_SECS = 5 * 60          # 5-minute rolling window for direction
_DIRECTION_THRESHOLD = 0.0001  # ~0.01% move to register a direction
_STALE_THRESHOLD_S = 15 * 60  # bar older than this → stale

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
    details: dict[str, str] = field(default_factory=dict)    # symbol → "↑"/"↓"/"?"
    stale_symbols: frozenset[str] = field(default_factory=frozenset)

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
        self._windows: dict[str, deque[tuple[datetime, float]]] = {
            t: deque() for t in self.TICKERS
        }
        # Timestamp of the most recent bar in each window (None if empty).
        self._last_bar_ts: dict[str, datetime | None] = {t: None for t in self.TICKERS}

    # ── public API ────────────────────────────────────────────────────────────

    def is_aligned(
        self,
        side: Literal["BUY", "SELL"],
        stale_threshold_s: int = _STALE_THRESHOLD_S,
    ) -> ConfirmationResult:
        now = datetime.now(UTC)
        aligned = 0
        stale: set[str] = set()
        details: dict[str, str] = {}

        for symbol in self.TICKERS:
            last_ts = self._last_bar_ts[symbol]
            if last_ts is not None and (now - last_ts).total_seconds() > stale_threshold_s:
                stale.add(symbol)

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
            stale_symbols=frozenset(stale),
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
                series = (
                    close[symbol].dropna()
                    if hasattr(close, "columns")
                    else close.dropna()
                )
                if series.empty:
                    continue
                all_bars = self._parse_series(series)
                self._update_window(symbol, all_bars, cutoff)
            except Exception as exc:
                log.warning("price_context_symbol_error", symbol=symbol, error=str(exc))

    @staticmethod
    def _parse_series(series: Any) -> list[tuple[datetime, float]]:
        bars: list[tuple[datetime, float]] = []
        for ts_idx, price_val in series.items():
            try:
                ts = ts_idx.to_pydatetime()
                ts = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
                bars.append((ts, float(price_val)))
            except Exception:
                continue
        return bars

    def _update_window(
        self,
        symbol: str,
        all_bars: list[tuple[datetime, float]],
        cutoff: datetime,
    ) -> None:
        in_window = [(ts, p) for ts, p in all_bars if ts >= cutoff]

        # If fewer than 2 points in the window, seed from the most recent
        # pre-window bars so _direction() can return a value immediately.
        needed = max(0, 2 - len(in_window))
        pre_window = [(ts, p) for ts, p in all_bars if ts < cutoff]
        seed = pre_window[-needed:] if needed else []

        dq = self._windows[symbol]
        dq.clear()
        dq.extend(seed + in_window)

        if dq:
            self._last_bar_ts[symbol] = dq[-1][0]
            log.debug(
                "price_context_updated",
                symbol=symbol,
                price=round(dq[-1][1], 4),
                points=len(dq),
                seeded=bool(seed),
            )

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
        if dq:
            self._last_bar_ts[symbol] = dq[-1][0]
