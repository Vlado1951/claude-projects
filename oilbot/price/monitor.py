from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from ib_insync import IB, Contract, Future, RealTimeBar, Ticker

from oilbot.config import Config

if TYPE_CHECKING:
    from oilbot.alerts.telegram import TelegramAlerter

log = structlog.get_logger()

_MIN_BACKOFF = 5.0
_MAX_BACKOFF = 300.0


@dataclass
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class PriceWindow:
    high: float
    low: float
    open: float
    close: float
    bars: list[Bar]


class PriceMonitor:
    def __init__(
        self,
        config: Config,
        alerter: TelegramAlerter | None = None,
    ) -> None:
        self._host = config.ibkr_host
        self._port = config.ibkr_port
        self._client_id = config.ibkr_client_id
        self._alerter = alerter

        # 30-min rolling buffer of 5-second bars (30*60/5 = 360 max bars)
        self._bars: deque[Bar] = deque(maxlen=360)
        # 5-min rolling buffer of (time, price) ticks (5*60 = 300 max ticks at 1/s)
        self._ticks: deque[tuple[datetime, float]] = deque(maxlen=300)

        self._ib = IB()  # type: ignore[no-untyped-call]
        self._contract: Future | None = None
        self._connected = False
        self._degraded_notified = False

    # ── public API ────────────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def last_price(self) -> float | None:
        if not self._ticks:
            return None
        return self._ticks[-1][1]

    def get_window(self, end: datetime, lookback_seconds: int) -> PriceWindow:
        cutoff = end.timestamp() - lookback_seconds
        bars = [b for b in self._bars if b.time.timestamp() >= cutoff and b.time <= end]
        if not bars:
            return PriceWindow(high=0.0, low=0.0, open=0.0, close=0.0, bars=[])
        return PriceWindow(
            high=max(b.high for b in bars),
            low=min(b.low for b in bars),
            open=bars[0].open,
            close=bars[-1].close,
            bars=bars,
        )

    # ── lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Connect, resolve front-month MCL, subscribe to bars and ticks.

        Runs until cancelled. Handles reconnection with exponential backoff.
        """
        backoff = _MIN_BACKOFF
        while True:
            try:
                await self._connect_and_subscribe()
                backoff = _MIN_BACKOFF
            except asyncio.CancelledError:
                await self._disconnect()
                raise
            except Exception as exc:
                self._connected = False
                log.error("ibkr_connection_error", error=str(exc), next_backoff=backoff)
                if self._alerter and not self._degraded_notified:
                    await self._alerter.system(
                        "price feed disconnected — alerts paused"
                    )
                    self._degraded_notified = True
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)

    async def stop(self) -> None:
        await self._disconnect()

    # ── internals ─────────────────────────────────────────────────────────────

    async def _connect_and_subscribe(self) -> None:
        log.info("ibkr_connecting", host=self._host, port=self._port)
        await self._ib.connectAsync(
            host=self._host,
            port=self._port,
            clientId=self._client_id,
            timeout=10,
        )
        self._connected = True
        log.info("ibkr_connected")

        if self._degraded_notified and self._alerter:
            await self._alerter.system("price feed reconnected — alerts resumed")
            self._degraded_notified = False

        contract = await self._resolve_front_month()
        self._contract = contract
        log.info(
            "ibkr_contract_resolved",
            symbol=contract.symbol,
            last_trade_date=contract.lastTradeDateOrContractMonth,
            exchange=contract.exchange,
        )

        self._subscribe_bars(contract)
        self._subscribe_ticks(contract)

        self._ib.disconnectedEvent += self._on_disconnect

        # Keep alive — ib_insync runs its own event loop integration
        while self._ib.isConnected():
            await asyncio.sleep(1)

    async def _resolve_front_month(self) -> Future:
        mcl = Future(symbol="MCL", exchange="NYMEX", currency="USD")
        details_list = await self._ib.reqContractDetailsAsync(mcl)
        if not details_list:
            raise RuntimeError(
                "No MCL contract details returned. "
                "Enable CL/MCL market data subscription in IBKR account."
            )
        # Sort by expiry, pick the nearest upcoming contract
        today = datetime.now(UTC).strftime("%Y%m%d")
        valid = [d for d in details_list if d.contract is not None]
        valid.sort(key=lambda d: d.contract.lastTradeDateOrContractMonth)  # type: ignore[union-attr]
        upcoming = [
            d for d in valid
            if d.contract.lastTradeDateOrContractMonth >= today  # type: ignore[union-attr]
        ]
        if not upcoming:
            upcoming = valid  # fall back to nearest if all appear expired
        chosen = upcoming[0].contract
        assert chosen is not None
        return Future(
            symbol=chosen.symbol,
            lastTradeDateOrContractMonth=chosen.lastTradeDateOrContractMonth,
            exchange=chosen.exchange,
            currency=chosen.currency,
        )

    def _subscribe_bars(self, contract: Contract) -> None:
        bars = self._ib.reqRealTimeBars(
            contract,
            barSize=5,
            whatToShow="TRADES",
            useRTH=False,
        )
        bars.updateEvent += self._on_bar

    def _subscribe_ticks(self, contract: Contract) -> None:
        ticker = self._ib.reqMktData(contract, genericTickList="", snapshot=False)
        ticker.updateEvent += self._on_tick

    def _on_bar(self, bars: Any, has_new_bar: bool) -> None:
        if not has_new_bar or not bars:
            return
        raw: RealTimeBar = bars[-1]
        # ib_insync: RealTimeBar.time is already a datetime; open field is open_
        bar = Bar(
            time=(
                raw.time if isinstance(raw.time, datetime)
                else datetime.fromtimestamp(raw.time, tz=UTC)
            ),
            open=float(raw.open_),
            high=float(raw.high),
            low=float(raw.low),
            close=float(raw.close),
            volume=float(raw.volume),
        )
        self._bars.append(bar)
        log.debug("ibkr_bar", close=bar.close, time=bar.time.isoformat())

    def _on_tick(self, ticker: Ticker) -> None:
        price = ticker.last
        if price is None or price != price:  # NaN check
            return
        now = datetime.now(UTC)
        self._ticks.append((now, float(price)))
        log.debug("ibkr_tick", price=float(price))

    def _on_disconnect(self) -> None:
        self._connected = False
        log.warning("ibkr_disconnected")

    async def _disconnect(self) -> None:
        if self._ib.isConnected():
            self._ib.disconnect()  # type: ignore[no-untyped-call]
        self._connected = False
