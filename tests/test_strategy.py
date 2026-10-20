"""Unit tests for strategy/window.py helpers and StrategyEngine state machine."""
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oilbot.news.models import NewsEvent
from oilbot.persistence.db import Database
from oilbot.price.types import Bar, PriceWindow
from oilbot.strategy.engine import StrategyEngine
from oilbot.strategy.window import (
    compute_atr,
    detect_bounce,
    detect_breakdown,
    detect_breakout,
    detect_pullback,
    find_dump_low,
    find_spike_high,
)
from oilbot.types import Classification, Sentiment

# ── helpers ────────────────────────────────────────────────────────────────────

T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)


def _bar(
    t_offset_s: float, close: float, high: float | None = None, low: float | None = None
) -> Bar:
    return Bar(
        time=T0 + timedelta(seconds=t_offset_s),
        open=close,
        high=high if high is not None else close,
        low=low if low is not None else close,
        close=close,
        volume=1.0,
    )


def _event(post_id: str = "test-post") -> NewsEvent:
    return NewsEvent(
        id=post_id,
        author="testuser",
        text="Oil news",
        posted_at=T0,
        received_at=T0,
        url="https://example.com",
    )


def _cls(sentiment: Sentiment) -> Classification:
    return Classification(sentiment=sentiment, confidence=0.9, rationale="test", source="keywords")


def _make_config(**kwargs: object) -> MagicMock:
    cfg = MagicMock()
    cfg.spike_window_seconds = kwargs.get("spike_window_seconds", 120)
    cfg.pullback_window_seconds = kwargs.get("pullback_window_seconds", 300)
    cfg.total_window_seconds = kwargs.get("total_window_seconds", 600)
    cfg.pullback_k = kwargs.get("pullback_k", 0.5)
    cfg.atr_bars = kwargs.get("atr_bars", 12)
    cfg.k_static = kwargs.get("k_static", 0.10)
    cfg.target_rr = kwargs.get("target_rr", 1.5)
    return cfg


def _make_db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


class FakePriceMonitor:
    def __init__(self, bars: list[Bar], connected: bool = True) -> None:
        self._bars = bars
        self._connected = connected

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def last_price(self) -> float | None:
        return self._bars[-1].close if self._bars else None

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


def _make_engine(
    bars: list[Bar],
    tmp_path: Path,
    alerter: AsyncMock | None = None,
    connected: bool = True,
) -> tuple[StrategyEngine, Database]:
    """Build an engine with a fake clock starting at T0, advancing via patched sleep."""
    db = _make_db(tmp_path)
    config = _make_config()
    monitor = FakePriceMonitor(bars, connected=connected)

    fake_time = [T0]

    async def fake_sleep(secs: float) -> None:
        fake_time[0] += timedelta(seconds=secs)

    engine = StrategyEngine(
        config,
        monitor,  # type: ignore[arg-type]
        db,
        alerter=alerter,
        _clock=lambda: fake_time[0],
        _poll_interval=10.0,
    )
    engine._fake_sleep = fake_sleep  # type: ignore[attr-defined]
    return engine, db


# ── window.py pure helpers ─────────────────────────────────────────────────────

def test_find_spike_high():
    bars = [_bar(0, 100.0), _bar(5, 103.0), _bar(10, 101.5)]
    assert find_spike_high(bars) == 103.0


def test_find_dump_low():
    bars = [_bar(0, 100.0), _bar(5, 97.0), _bar(10, 98.5)]
    assert find_dump_low(bars) == 97.0


def test_compute_atr_none_if_too_few():
    assert compute_atr([_bar(0, 100.0)]) is None


def test_compute_atr_basic():
    bars = [
        _bar(0, 100.0, high=101.0, low=99.0),
        _bar(5, 101.0, high=102.0, low=100.0),
        _bar(10, 100.5, high=101.5, low=99.5),
    ]
    atr = compute_atr(bars)
    assert atr is not None
    assert atr > 0


def test_detect_pullback_threshold():
    bars = [_bar(0, 102.5), _bar(5, 99.8)]  # 99.8 < 100.0 - 0.10
    result = detect_pullback(bars, spike_high=100.0, threshold=0.10)
    assert result == pytest.approx(99.8)


def test_detect_pullback_lower_lows():
    bars = [
        _bar(0, 99.5, low=99.5),
        _bar(5, 99.3, low=99.3),
        _bar(10, 99.0, low=99.0),  # 3-bar lower-lows
        _bar(15, 98.8),
    ]
    # threshold high so method 1 doesn't fire
    result = detect_pullback(bars, spike_high=100.0, threshold=5.0)
    assert result is not None
    assert result <= 98.8


def test_detect_pullback_none():
    bars = [_bar(0, 99.95), _bar(5, 99.96)]  # never below 100 - 0.10 = 99.90
    result = detect_pullback(bars, spike_high=100.0, threshold=0.10)
    assert result is None


def test_detect_breakout_found():
    bars = [_bar(0, 99.5), _bar(5, 100.05)]
    assert detect_breakout(bars, spike_high=100.0) == pytest.approx(100.05)


def test_detect_breakout_none():
    bars = [_bar(0, 99.5), _bar(5, 99.9)]
    assert detect_breakout(bars, spike_high=100.0) is None


def test_detect_bounce_threshold():
    bars = [_bar(0, 97.5), _bar(5, 100.2)]  # 100.2 > 100.0 + 0.10
    result = detect_bounce(bars, dump_low=100.0, threshold=0.10)
    assert result is not None


def test_detect_breakdown_found():
    bars = [_bar(0, 100.5), _bar(5, 99.95)]
    assert detect_breakdown(bars, dump_low=100.0) == pytest.approx(99.95)


def test_detect_breakdown_none():
    bars = [_bar(0, 100.05), _bar(5, 100.1)]
    assert detect_breakdown(bars, dump_low=100.0) is None


# ── StrategyEngine state machine ───────────────────────────────────────────────

async def _run_engine(engine: StrategyEngine, event: NewsEvent, cls: Classification) -> None:
    with patch("asyncio.sleep", new=engine._fake_sleep):  # type: ignore[attr-defined]
        await engine.handle(event, cls)
        # wait for the background window task
        if engine._tasks:
            await asyncio.gather(*list(engine._tasks), return_exceptions=True)


async def test_bullish_breakout_fires(tmp_path: Path) -> None:
    alerter = AsyncMock()
    # spike window: T+60s bar at 100.0 (H = 100.0, k_static used)
    # pullback: T+200s bar at 99.85 (< 100.0 - 0.10) → L = 99.85
    # breakout: T+500s bar at 100.05 (> 100.0)
    bars = [
        _bar(60, 100.0),
        _bar(200, 99.85),
        _bar(500, 100.05),
    ]
    engine, db = _make_engine(bars, tmp_path, alerter=alerter)
    await _run_engine(engine, _event(), _cls(Sentiment.BULLISH))

    alerter.alert.assert_called_once()
    payload = alerter.alert.call_args[0][0]
    assert payload.side == "BUY"
    assert payload.spike_level == pytest.approx(100.0)
    assert payload.pullback_level == pytest.approx(99.85)
    assert payload.breakout_level == pytest.approx(100.05)
    assert payload.stop == pytest.approx(99.85)
    # target = 100.05 + 1.5 * (100.05 - 99.85) = 100.05 + 0.30 = 100.35
    assert payload.target == pytest.approx(100.35)
    # verify recorded in DB
    summary = db.last_24h_summary()
    assert summary["alerts_sent"] == 1


async def test_bullish_no_pullback_timeout(tmp_path: Path) -> None:
    alerter = AsyncMock()
    # spike: T+60s at 100.0; post-spike bars never dip below 99.90
    bars = [_bar(60, 100.0), _bar(200, 99.95), _bar(400, 99.95)]
    engine, db = _make_engine(bars, tmp_path, alerter=alerter)

    import structlog.testing
    with structlog.testing.capture_logs() as cap:
        await _run_engine(engine, _event(), _cls(Sentiment.BULLISH))

    alerter.alert.assert_not_called()
    assert any(e.get("reason") == "no_pullback" for e in cap)


async def test_bullish_pullback_no_breakout_timeout(tmp_path: Path) -> None:
    alerter = AsyncMock()
    # spike: T+60s at 100.0; pullback: T+200s at 99.85; no breakout
    bars = [_bar(60, 100.0), _bar(200, 99.85), _bar(500, 99.95)]
    engine, db = _make_engine(bars, tmp_path, alerter=alerter)

    import structlog.testing
    with structlog.testing.capture_logs() as cap:
        await _run_engine(engine, _event(), _cls(Sentiment.BULLISH))

    alerter.alert.assert_not_called()
    assert any(e.get("reason") == "no_breakout" for e in cap)


async def test_bearish_breakdown_fires(tmp_path: Path) -> None:
    alerter = AsyncMock()
    # dump window: T+60s bar at 100.0 (L = 100.0)
    # bounce: T+200s at 100.15 (> 100.0 + 0.10) → H_bounce = 100.15
    # breakdown: T+500s at 99.95 (< 100.0)
    bars = [
        _bar(60, 100.0),
        _bar(200, 100.15),
        _bar(500, 99.95),
    ]
    engine, db = _make_engine(bars, tmp_path, alerter=alerter)
    await _run_engine(engine, _event(), _cls(Sentiment.BEARISH))

    alerter.alert.assert_called_once()
    payload = alerter.alert.call_args[0][0]
    assert payload.side == "SELL"
    assert payload.spike_level == pytest.approx(100.0)   # dump low
    assert payload.pullback_level == pytest.approx(100.15)  # bounce high
    assert payload.breakout_level == pytest.approx(99.95)  # breakdown close
    assert payload.stop == pytest.approx(100.15)
    # target = 99.95 - 1.5 * (100.15 - 99.95) = 99.95 - 0.30 = 99.65
    assert payload.target == pytest.approx(99.65)


async def test_bearish_no_bounce_timeout(tmp_path: Path) -> None:
    alerter = AsyncMock()
    # dump: T+60s at 100.0; post-dump bars never bounce above 100.10
    bars = [_bar(60, 100.0), _bar(200, 100.05), _bar(400, 100.08)]
    engine, db = _make_engine(bars, tmp_path, alerter=alerter)

    import structlog.testing
    with structlog.testing.capture_logs() as cap:
        await _run_engine(engine, _event(), _cls(Sentiment.BEARISH))

    alerter.alert.assert_not_called()
    assert any(e.get("reason") == "no_bounce" for e in cap)


async def test_neutral_event_ignored(tmp_path: Path) -> None:
    alerter = AsyncMock()
    engine, _ = _make_engine([], tmp_path, alerter=alerter)
    with patch("asyncio.sleep", new=engine._fake_sleep):  # type: ignore[attr-defined]
        await engine.handle(_event(), _cls(Sentiment.NEUTRAL))
    assert not engine._tasks
    alerter.alert.assert_not_called()


async def test_no_bars_in_spike_window(tmp_path: Path) -> None:
    """Engine handles the case where no bars are available during the spike window."""
    alerter = AsyncMock()
    engine, _ = _make_engine([], tmp_path, alerter=alerter)

    import structlog.testing
    with structlog.testing.capture_logs() as cap:
        await _run_engine(engine, _event(), _cls(Sentiment.BULLISH))

    alerter.alert.assert_not_called()
    assert any("no_bars" in str(e.get("reason", "")) for e in cap)


async def test_concurrency_capped_at_five(tmp_path: Path) -> None:
    """At most 5 strategy windows run concurrently; 6th waits for semaphore."""
    engine, _ = _make_engine([], tmp_path)

    concurrent = 0
    peak = 0

    async def slow_run(event: NewsEvent, cls: Classification, T: datetime) -> None:
        nonlocal concurrent, peak
        concurrent += 1
        peak = max(peak, concurrent)
        await asyncio.sleep(0.05)
        concurrent -= 1

    with (
        patch.object(engine, "_run_bullish", new=slow_run),
        patch.object(engine, "_run_bearish", new=slow_run),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        cls = _cls(Sentiment.BULLISH)
        for i in range(6):
            await engine.handle(_event(str(i)), cls)
        await asyncio.gather(*list(engine._tasks), return_exceptions=True)

    assert peak <= 5
