from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from oilbot.config import Config
from oilbot.persistence.outcomes import OutcomesStore
from oilbot.types import AlertPayload

if TYPE_CHECKING:
    from oilbot.alerts.telegram import TelegramAlerter

log = structlog.get_logger()


@dataclass
class _OpenAlert:
    alert_id: int
    side: str
    entry: float
    stop: float
    target: float
    tracked_from: datetime
    fill_price: float | None = None
    fill_at: datetime | None = None
    mfe_price: float | None = None
    mae_price: float | None = None
    paused_seconds: int = 0
    paused_since: datetime | None = field(default=None, compare=False)


class OutcomeTracker:
    """Tracks fired alerts against live price ticks and resolves them to outcomes."""

    def __init__(
        self,
        config: Config,
        db_path: Path,
        alerter: TelegramAlerter | None = None,
    ) -> None:
        self._timeout_s = config.outcome_timeout_minutes * 60
        self._fill_model = config.fill_model
        self._slippage_ticks = config.stop_slippage_ticks
        self._tick_size = config.tick_size
        self._send_telegram = config.outcome_telegram
        self._outcomes = OutcomesStore(db_path)
        self._alerter = alerter
        self._open: dict[int, _OpenAlert] = {}
        self._paused = False
        self._last_tick_price: float | None = None
        self._tick_queue: asyncio.Queue[tuple[float, datetime]] = asyncio.Queue()

    @property
    def open_count(self) -> int:
        return len(self._open)

    # ── public API ────────────────────────────────────────────────────────────

    def open_alert(self, alert_id: int, payload: AlertPayload) -> None:
        """Register a newly-fired alert for in-memory outcome tracking."""
        now = datetime.now(UTC)
        alert = _OpenAlert(
            alert_id=alert_id,
            side=payload.side,
            entry=payload.breakout_level,
            stop=payload.stop,
            target=payload.target,
            tracked_from=now,
        )
        if self._fill_model == "theoretical":
            alert.fill_price = payload.breakout_level
            alert.fill_at = now
            self._outcomes.update_outcome(
                alert_id,
                fill_price=payload.breakout_level,
                fill_at=now.isoformat(),
            )
        self._open[alert_id] = alert
        log.info(
            "outcome_tracking_started",
            alert_id=alert_id,
            side=payload.side,
            entry=payload.breakout_level,
            stop=payload.stop,
            target=payload.target,
            fill_model=self._fill_model,
        )

    def handle_tick(self, price: float, ts: datetime | None = None) -> None:
        """Deliver a price tick. Called from PriceMonitor's tick callback (sync-safe)."""
        self._tick_queue.put_nowait((price, ts or datetime.now(UTC)))

    def handle_disconnect(self) -> None:
        """Pause all in-flight outcome tracking while IBKR is disconnected."""
        if self._paused:
            return
        self._paused = True
        now = datetime.now(UTC)
        for alert in self._open.values():
            alert.paused_since = now
        log.info("outcome_paused", open_count=len(self._open))

    def handle_reconnect(self) -> None:
        """Resume tracking after IBKR reconnects and accumulate paused time."""
        if not self._paused:
            return
        self._paused = False
        now = datetime.now(UTC)
        for alert in self._open.values():
            if alert.paused_since is not None:
                alert.paused_seconds += int((now - alert.paused_since).total_seconds())
                alert.paused_since = None
        log.info("outcome_resumed", open_count=len(self._open))

    async def start(self) -> None:
        """Load open outcomes from DB, then process the live tick stream."""
        await self._resume_from_db()
        while True:
            try:
                price, ts = await asyncio.wait_for(self._tick_queue.get(), timeout=5.0)
            except TimeoutError:
                await self._check_timeouts(datetime.now(UTC))
                continue
            self._last_tick_price = price
            if not self._paused:
                await self._process_tick(price, ts)
            await self._check_timeouts(ts)

    # ── internals ────────────────────────────────────────────────────────────

    async def _resume_from_db(self) -> None:
        now = datetime.now(UTC)
        stale_cutoff_s = 2 * self._timeout_s
        rows = self._outcomes.list_open_with_alerts()

        for row in rows:
            alert_id: int = row["alert_id"]
            tracked_from = datetime.fromisoformat(row["tracked_from_at"])
            age_s = (now - tracked_from).total_seconds()

            if age_s >= stale_cutoff_s:
                # Use the last recorded price from MFE/MAE as a proxy
                last_known: float | None = row.get("mfe_price") or row.get("mae_price")
                realized_r = (
                    self._r(row["side"], row["entry"], row["stop"], last_known)
                    if last_known is not None
                    else None
                )
                log.warning(
                    "outcome_stale_force_closed",
                    alert_id=alert_id,
                    age_s=round(age_s),
                )
                self._outcomes.close_outcome(
                    alert_id,
                    status="timeout",
                    realized_r=realized_r,
                    tracked_until_at=now,
                )
                continue

            alert = _OpenAlert(
                alert_id=alert_id,
                side=row["side"],
                entry=row["entry"],
                stop=row["stop"],
                target=row["target"],
                tracked_from=tracked_from,
                fill_price=row.get("fill_price"),
                fill_at=(
                    datetime.fromisoformat(row["fill_at"])
                    if row.get("fill_at") else None
                ),
                mfe_price=row.get("mfe_price"),
                mae_price=row.get("mae_price"),
                paused_seconds=row.get("paused_seconds") or 0,
            )
            self._open[alert_id] = alert
            log.info("outcome_tracking_started", alert_id=alert_id, resumed=True)

    async def _process_tick(self, price: float, ts: datetime) -> None:
        to_close: list[tuple[int, str, float]] = []

        for alert_id, alert in list(self._open.items()):
            # ── fill detection (next_tick mode) ───────────────────────────
            if alert.fill_price is None:
                filled = (
                    (alert.side == "BUY" and price >= alert.entry)
                    or (alert.side == "SELL" and price <= alert.entry)
                )
                if filled:
                    alert.fill_price = price
                    alert.fill_at = ts
                    self._outcomes.update_outcome(
                        alert_id,
                        fill_price=price,
                        fill_at=ts.isoformat(),
                    )
                    log.info(
                        "outcome_filled",
                        alert_id=alert_id,
                        fill_price=price,
                        side=alert.side,
                    )

            if alert.fill_price is None:
                continue  # not yet filled; don't track MFE/MAE or check levels

            # ── MFE / MAE ─────────────────────────────────────────────────
            self._update_excursion(alert, price)

            # ── terminal condition check ───────────────────────────────────
            if alert.side == "BUY":
                if price >= alert.target:
                    to_close.append((alert_id, "target_hit", alert.target))
                elif price <= alert.stop:
                    exit_p = alert.stop - self._slippage_ticks * self._tick_size
                    to_close.append((alert_id, "stop_hit", exit_p))
            else:
                if price <= alert.target:
                    to_close.append((alert_id, "target_hit", alert.target))
                elif price >= alert.stop:
                    exit_p = alert.stop + self._slippage_ticks * self._tick_size
                    to_close.append((alert_id, "stop_hit", exit_p))

        for alert_id, status, exit_price in to_close:
            await self._close(alert_id, status, exit_price, ts)

    async def _check_timeouts(self, now: datetime) -> None:
        to_close = []
        for alert_id, alert in list(self._open.items()):
            effective_s = (now - alert.tracked_from).total_seconds() - alert.paused_seconds
            if effective_s >= self._timeout_s:
                to_close.append(alert_id)

        for alert_id in to_close:
            maybe = self._open.get(alert_id)
            if maybe is None:
                continue
            alert = maybe
            if alert.fill_price is not None:
                last = self._last_tick_price if self._last_tick_price is not None else alert.entry
                exit_price = last
                realized_r: float | None = self._r(alert.side, alert.entry, alert.stop, exit_price)
            else:
                exit_price = alert.entry
                realized_r = None
            await self._close(alert_id, "timeout", exit_price, now, realized_r=realized_r)

    async def _close(
        self,
        alert_id: int,
        status: str,
        exit_price: float,
        ts: datetime,
        realized_r: float | None = None,
    ) -> None:
        alert = self._open.pop(alert_id, None)
        if alert is None:
            return

        if realized_r is None and alert.fill_price is not None:
            realized_r = self._r(alert.side, alert.entry, alert.stop, exit_price)

        mfe_r = (
            self._r(alert.side, alert.entry, alert.stop, alert.mfe_price)
            if alert.mfe_price is not None else None
        )
        mae_r = (
            self._r(alert.side, alert.entry, alert.stop, alert.mae_price)
            if alert.mae_price is not None else None
        )

        self._outcomes.close_outcome(
            alert_id,
            status=status,
            exit_price=exit_price,
            exit_at=ts,
            realized_r=realized_r,
            mfe_r=mfe_r,
            mae_r=mae_r,
            mfe_price=alert.mfe_price,
            mae_price=alert.mae_price,
            tracked_until_at=ts,
        )

        log.info(
            "outcome_resolved",
            alert_id=alert_id,
            status=status,
            realized_r=round(realized_r, 4) if realized_r is not None else None,
            mfe_r=round(mfe_r, 4) if mfe_r is not None else None,
            mae_r=round(mae_r, 4) if mae_r is not None else None,
        )

        if self._send_telegram and self._alerter:
            await self._alerter.system(
                self._format_outcome(alert, status, exit_price, ts, realized_r, mfe_r, mae_r)
            )

    def _update_excursion(self, alert: _OpenAlert, price: float) -> None:
        if alert.side == "BUY":
            if alert.mfe_price is None or price > alert.mfe_price:
                alert.mfe_price = price
            if alert.mae_price is None or price < alert.mae_price:
                alert.mae_price = price
        else:
            if alert.mfe_price is None or price < alert.mfe_price:
                alert.mfe_price = price
            if alert.mae_price is None or price > alert.mae_price:
                alert.mae_price = price

    @staticmethod
    def _r(side: str, entry: float, stop: float, exit_price: float) -> float:
        risk = abs(entry - stop)
        if risk == 0.0:
            return 0.0
        if side == "BUY":
            return (exit_price - entry) / risk
        return (entry - exit_price) / risk

    @staticmethod
    def _format_outcome(
        alert: _OpenAlert,
        status: str,
        exit_price: float,
        exit_at: datetime,
        realized_r: float | None,
        mfe_r: float | None,
        mae_r: float | None,
    ) -> str:
        triggered_at = alert.fill_at or alert.tracked_from
        fill = alert.fill_price or alert.entry
        r_str = f"{realized_r:+.2f}R" if realized_r is not None else "n/a"
        mfe_str = f"{mfe_r:+.2f}R" if mfe_r is not None else "n/a"
        mae_str = f"{mae_r:+.2f}R" if mae_r is not None else "n/a"
        return (
            f"📊 Alert outcome — {alert.side} MCL\n"
            f"Triggered: {triggered_at.strftime('%H:%M:%S UTC')} @ {fill:.2f}\n"
            f"Result: {status} ({r_str}) at {exit_at.strftime('%H:%M:%S UTC')}\n"
            f"MFE: {mfe_str}, MAE: {mae_str}"
        )
