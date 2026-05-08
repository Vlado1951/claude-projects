from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import structlog

from oilbot.config import Config
from oilbot.news.models import NewsEvent
from oilbot.outcomes.tracker import OutcomeTracker
from oilbot.persistence.db import Database
from oilbot.persistence.outcomes import OutcomesStore
from oilbot.price.context import PriceContext
from oilbot.price.types import Bar, PriceSource
from oilbot.strategy.window import (
    compute_atr,
    detect_bounce,
    detect_breakdown,
    detect_breakout,
    detect_pullback,
    find_dump_low,
    find_spike_high,
)
from oilbot.types import AlertPayload, Classification, Sentiment

if TYPE_CHECKING:
    from oilbot.alerts.telegram import TelegramAlerter

log = structlog.get_logger()

_MAX_CONCURRENT = 5
_POLL_INTERVAL = 1.0


class StrategyEngine:
    def __init__(
        self,
        config: Config,
        monitor: PriceSource,
        db: Database,
        alerter: TelegramAlerter | None = None,
        _clock: Callable[[], datetime] | None = None,
        _poll_interval: float = _POLL_INTERVAL,
        price_context: PriceContext | None = None,
        outcome_tracker: OutcomeTracker | None = None,
    ) -> None:
        self._spike_window_s = config.spike_window_seconds
        self._pullback_window_s = config.pullback_window_seconds
        self._total_window_s = config.total_window_seconds
        self._k = config.pullback_k
        self._atr_bars = config.atr_bars
        self._k_static = config.k_static
        self._target_rr = config.target_rr
        self._confirmation_mode = config.confirmation_mode

        self._monitor = monitor
        self._db = db
        self._outcomes = OutcomesStore(db._path)
        self._alerter = alerter
        self._clock = _clock or (lambda: datetime.now(UTC))
        self._poll_interval = _poll_interval
        self._price_context = price_context
        self._outcome_tracker = outcome_tracker

        self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT)
        self._tasks: set[asyncio.Task[None]] = set()

    async def handle(self, event: NewsEvent, classification: Classification) -> None:
        if classification.sentiment == Sentiment.NEUTRAL:
            return
        task = asyncio.create_task(self._run_guarded(event, classification))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self, timeout: float = 30.0) -> None:
        """Wait for all in-flight tasks to finish (used during clean shutdown)."""
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=timeout)

    # ── internals ─────────────────────────────────────────────────────────────

    async def _run_guarded(
        self, event: NewsEvent, classification: Classification
    ) -> None:
        async with self._semaphore:
            T = self._clock()
            try:
                if classification.sentiment == Sentiment.BULLISH:
                    await self._run_bullish(event, classification, T)
                else:
                    await self._run_bearish(event, classification, T)
            except Exception as exc:
                log.error("strategy_error", post_id=event.id, error=str(exc))

    def _bars_in(self, start: datetime, end: datetime) -> list[Bar]:
        lookback = max(1, int((end - start).total_seconds()) + 10)
        window = self._monitor.get_window(end, lookback)
        return [b for b in window.bars if b.time >= start]

    async def _run_bullish(
        self, event: NewsEvent, classification: Classification, T: datetime
    ) -> None:
        spike_deadline = T + timedelta(seconds=self._spike_window_s)
        pullback_deadline = T + timedelta(seconds=self._spike_window_s + self._pullback_window_s)
        total_deadline = T + timedelta(seconds=self._total_window_s)
        ctx = {"post_id": event.id, "side": "BUY"}

        # ── phase 1: wait for spike window to close ───────────────────────────
        while self._clock() < spike_deadline:
            if not self._monitor.is_connected:
                log.warning("strategy_paused_disconnected", **ctx)
                return
            await asyncio.sleep(self._poll_interval)

        spike_bars = self._bars_in(T, spike_deadline)
        if not spike_bars:
            log.info("no_setup", **ctx, reason="no_bars_in_spike_window")
            return

        H = find_spike_high(spike_bars)
        atr = compute_atr(spike_bars, self._atr_bars)
        threshold = self._k * atr if atr is not None else self._k_static
        log.info("spike_high_found", **ctx, spike_high=H, threshold=round(threshold, 4))

        # ── phase 2: look for pullback ─────────────────────────────────────────
        L: float | None = None
        while self._clock() < pullback_deadline:
            if not self._monitor.is_connected:
                log.warning("strategy_paused_disconnected", **ctx)
                return
            await asyncio.sleep(self._poll_interval)
            post_spike = self._bars_in(spike_deadline, self._clock())
            L = detect_pullback(post_spike, H, threshold)
            if L is not None:
                log.info("pullback_found", **ctx, pullback_low=L)
                break

        if L is None:
            log.info("no_setup", **ctx, reason="no_pullback")
            return

        # ── phase 3: look for breakout ─────────────────────────────────────────
        breakout: float | None = None
        while self._clock() < total_deadline:
            if not self._monitor.is_connected:
                log.warning("strategy_paused_disconnected", **ctx)
                return
            await asyncio.sleep(self._poll_interval)
            post_spike = self._bars_in(spike_deadline, self._clock())
            breakout = detect_breakout(post_spike, H)
            if breakout is not None:
                break

        if breakout is None:
            log.info("no_setup", **ctx, reason="no_breakout")
            return

        await self._fire_alert(event, classification, "BUY", H, L, breakout)

    async def _run_bearish(
        self, event: NewsEvent, classification: Classification, T: datetime
    ) -> None:
        spike_deadline = T + timedelta(seconds=self._spike_window_s)
        pullback_deadline = T + timedelta(seconds=self._spike_window_s + self._pullback_window_s)
        total_deadline = T + timedelta(seconds=self._total_window_s)
        ctx = {"post_id": event.id, "side": "SELL"}

        # ── phase 1: wait for dump window to close ────────────────────────────
        while self._clock() < spike_deadline:
            if not self._monitor.is_connected:
                log.warning("strategy_paused_disconnected", **ctx)
                return
            await asyncio.sleep(self._poll_interval)

        dump_bars = self._bars_in(T, spike_deadline)
        if not dump_bars:
            log.info("no_setup", **ctx, reason="no_bars_in_dump_window")
            return

        L = find_dump_low(dump_bars)
        atr = compute_atr(dump_bars, self._atr_bars)
        threshold = self._k * atr if atr is not None else self._k_static
        log.info("dump_low_found", **ctx, dump_low=L, threshold=round(threshold, 4))

        # ── phase 2: look for bounce ───────────────────────────────────────────
        H_bounce: float | None = None
        while self._clock() < pullback_deadline:
            if not self._monitor.is_connected:
                log.warning("strategy_paused_disconnected", **ctx)
                return
            await asyncio.sleep(self._poll_interval)
            post_dump = self._bars_in(spike_deadline, self._clock())
            H_bounce = detect_bounce(post_dump, L, threshold)
            if H_bounce is not None:
                log.info("bounce_found", **ctx, bounce_high=H_bounce)
                break

        if H_bounce is None:
            log.info("no_setup", **ctx, reason="no_bounce")
            return

        # ── phase 3: look for breakdown ────────────────────────────────────────
        breakdown: float | None = None
        while self._clock() < total_deadline:
            if not self._monitor.is_connected:
                log.warning("strategy_paused_disconnected", **ctx)
                return
            await asyncio.sleep(self._poll_interval)
            post_dump = self._bars_in(spike_deadline, self._clock())
            breakdown = detect_breakdown(post_dump, L)
            if breakdown is not None:
                break

        if breakdown is None:
            log.info("no_setup", **ctx, reason="no_breakdown")
            return

        await self._fire_alert(event, classification, "SELL", L, H_bounce, breakdown)

    async def _fire_alert(
        self,
        event: NewsEvent,
        classification: Classification,
        side: str,
        spike_level: float,
        pullback_level: float,
        breakout_level: float,
    ) -> None:
        entry = breakout_level
        stop = pullback_level
        if side == "BUY":
            target = entry + self._target_rr * (entry - stop)
        else:
            target = entry - self._target_rr * (stop - entry)

        # ── confirmation check ────────────────────────────────────────────────
        conf_status = "not_checked"
        conf_details = ""
        if self._price_context is not None and self._confirmation_mode != "off":
            result = self._price_context.is_aligned(side)  # type: ignore[arg-type]
            conf_status = result.status
            conf_details = result.summary()
            log.info(
                "confirmation_checked",
                post_id=event.id,
                side=side,
                status=conf_status,
                aligned=result.aligned_count,
            )
            if self._confirmation_mode == "required" and conf_status == "disaligned":
                msg = (
                    f"Setup detected ({side}) but confirmation disaligned "
                    f"— {conf_details}. Not firing."
                )
                log.info("alert_suppressed_confirmation", post_id=event.id)
                if self._alerter:
                    await self._alerter.system(msg)
                return

        payload = AlertPayload(
            side=side,  # type: ignore[arg-type]
            post_id=event.id,
            post_text=event.text,
            sentiment=classification.sentiment,
            confidence=classification.confidence,
            spike_level=spike_level,
            pullback_level=pullback_level,
            breakout_level=entry,
            breakout_at=self._clock(),
            stop=stop,
            target=target,
            confirmation_status=conf_status,
            confirmation_details=conf_details,
        )
        log.info(
            "alert_fired",
            post_id=event.id,
            side=side,
            entry=entry,
            stop=stop,
            target=round(target, 4),
            confirmation=conf_status,
        )
        if self._alerter:
            await self._alerter.alert(payload)
        alert_id = self._db.record_alert(
            post_id=event.id,
            side=side,
            entry=entry,
            stop=stop,
            target=target,
            confirmation_status=conf_status,
        )
        self._outcomes.open_outcome(alert_id, tracked_from_at=self._clock())
        if self._outcome_tracker is not None:
            self._outcome_tracker.open_alert(alert_id, payload)
