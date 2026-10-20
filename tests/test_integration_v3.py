"""Integration tests: news → alert → price ticks → resolved outcome (Phase 22)."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from oilbot.news.models import NewsEvent
from oilbot.outcomes.tracker import OutcomeTracker
from oilbot.persistence.db import Database
from oilbot.persistence.outcomes import OutcomesStore
from oilbot.price.types import Bar, PriceWindow
from oilbot.strategy.engine import StrategyEngine
from oilbot.types import Classification, Sentiment

# ── shared fixtures ───────────────────────────────────────────────────────────

T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)


def _bar(offset_s: float, close: float) -> Bar:
    return Bar(
        time=T0 + timedelta(seconds=offset_s),
        open=close, high=close, low=close, close=close, volume=1.0,
    )


# bars that produce: spike=100.0, pullback=99.85, breakout=100.05
# → entry=100.05, stop=99.85, risk=0.20, target=100.35 (1.5R)
_TRIGGER_BARS = [_bar(60, 100.0), _bar(200, 99.85), _bar(500, 100.05)]


class _FakeMonitor:
    def __init__(self, bars: list[Bar]) -> None:
        self._bars = bars

    @property
    def is_connected(self) -> bool:
        return True

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


def _engine_config() -> object:
    from unittest.mock import MagicMock
    cfg = MagicMock()
    cfg.spike_window_seconds = 120
    cfg.pullback_window_seconds = 300
    cfg.total_window_seconds = 600
    cfg.pullback_k = 0.5
    cfg.atr_bars = 12
    cfg.k_static = 0.10
    cfg.target_rr = 1.5
    cfg.confirmation_mode = "off"
    return cfg


def _tracker_config() -> object:
    from oilbot.config import Config
    return Config(
        telegram_token="tok",
        telegram_chat_id="123",
        outcome_timeout_minutes=60,
        fill_model="next_tick",
        stop_slippage_ticks=0,
        tick_size=0.01,
        outcome_telegram=False,
    )


def _bullish_event() -> NewsEvent:
    return NewsEvent(
        id="v3-integ-bullish-1",
        author="realDonaldTrump",
        text="OPEC announces production cut",
        posted_at=T0,
        received_at=T0,
        url="",
    )


def _bullish_cls() -> Classification:
    return Classification(
        sentiment=Sentiment.BULLISH,
        confidence=0.9,
        rationale="OPEC cut",
        source="keywords",
    )


def _setup(
    tmp_path: Path,
) -> tuple[StrategyEngine, OutcomeTracker, OutcomesStore, AsyncMock]:
    alerter = AsyncMock()
    db_path = tmp_path / "v3_integ.db"
    db = Database(db_path)
    store = OutcomesStore(db_path)
    tracker = OutcomeTracker(_tracker_config(), db_path)  # type: ignore[arg-type]

    fake_time = [T0]

    async def fake_sleep(secs: float) -> None:
        fake_time[0] += timedelta(seconds=secs)

    engine = StrategyEngine(
        _engine_config(),  # type: ignore[arg-type]
        _FakeMonitor(_TRIGGER_BARS),  # type: ignore[arg-type]
        db,
        alerter=alerter,
        outcome_tracker=tracker,
        _clock=lambda: fake_time[0],
        _poll_interval=10.0,
    )
    engine._fake_sleep = fake_sleep  # type: ignore[attr-defined]
    return engine, tracker, store, alerter


# ── helpers ───────────────────────────────────────────────────────────────────


async def _fire_alert(
    engine: StrategyEngine, event: NewsEvent, cls: Classification
) -> None:
    with patch("asyncio.sleep", new=engine._fake_sleep):  # type: ignore[attr-defined]
        await engine.handle(event, cls)
        await asyncio.gather(*list(engine._tasks), return_exceptions=True)


# ── tests ─────────────────────────────────────────────────────────────────────


async def test_bullish_news_spike_to_target_hit(tmp_path: Path) -> None:
    """
    Full pipeline:
      BULLISH news → spike/pullback/breakout → BUY alert fired
      → price ticks cross entry, rise to target → target_hit outcome, realized_r ≈ 1.5
    """
    engine, tracker, store, alerter = _setup(tmp_path)

    await _fire_alert(engine, _bullish_event(), _bullish_cls())

    # Alert should have fired with correct levels
    alerter.alert.assert_called_once()
    payload = alerter.alert.call_args[0][0]
    assert payload.side == "BUY"
    assert payload.breakout_level == pytest.approx(100.05)
    assert payload.stop == pytest.approx(99.85)
    assert payload.target == pytest.approx(100.35)

    # Tracker registered the open alert
    assert tracker.open_count == 1

    # Simulate price ticks: fill → rally → target
    now = datetime.now(UTC)
    await tracker._process_tick(100.06, now)  # fill (>= entry 100.05)
    await tracker._process_tick(100.20, now)  # MFE builds
    await tracker._process_tick(100.35, now)  # hits target exactly

    # Outcome resolved
    assert tracker.open_count == 0

    closed = store.list_closed()
    assert len(closed) == 1
    row = closed[0]
    assert row["status"] == "target_hit"
    assert row["realized_r"] == pytest.approx(1.5, abs=0.01)
    assert row["mfe_r"] is not None
    assert row["mfe_r"] >= 1.49
    assert row["exit_price"] == pytest.approx(100.35)


async def test_bullish_news_spike_to_stop_hit(tmp_path: Path) -> None:
    """
    Full pipeline:
      BULLISH news → BUY alert fired
      → price fills then reverses to stop → stop_hit outcome, realized_r ≈ -1.0
    """
    engine, tracker, store, alerter = _setup(tmp_path)

    await _fire_alert(engine, _bullish_event(), _bullish_cls())

    alerter.alert.assert_called_once()
    payload = alerter.alert.call_args[0][0]
    assert payload.side == "BUY"

    assert tracker.open_count == 1

    now = datetime.now(UTC)
    await tracker._process_tick(100.06, now)  # fill
    await tracker._process_tick(100.10, now)  # brief MFE
    await tracker._process_tick(99.85, now)   # reverses to stop

    assert tracker.open_count == 0

    closed = store.list_closed()
    assert len(closed) == 1
    row = closed[0]
    assert row["status"] == "stop_hit"
    assert row["realized_r"] == pytest.approx(-1.0, abs=0.01)
    assert row["mae_r"] is not None
    assert row["mae_r"] <= -0.99


async def test_outcome_row_written_immediately_on_alert_fire(tmp_path: Path) -> None:
    """The alert_outcomes row exists with status=tracking as soon as the alert fires."""
    engine, tracker, store, alerter = _setup(tmp_path)

    await _fire_alert(engine, _bullish_event(), _bullish_cls())

    alerter.alert.assert_called_once()
    open_rows = store.list_open()
    assert len(open_rows) == 1
    assert open_rows[0]["status"] == "tracking"


async def test_no_alert_no_outcome_row(tmp_path: Path) -> None:
    """If no setup forms (no bars), no alert fires and no outcome row is created."""
    alerter = AsyncMock()
    db_path = tmp_path / "v3_no_alert.db"
    db = Database(db_path)
    store = OutcomesStore(db_path)
    tracker = OutcomeTracker(_tracker_config(), db_path)  # type: ignore[arg-type]

    fake_time = [T0]

    async def fake_sleep(secs: float) -> None:
        fake_time[0] += timedelta(seconds=secs)

    engine = StrategyEngine(
        _engine_config(),  # type: ignore[arg-type]
        _FakeMonitor([]),  # no bars → no setup
        db,
        alerter=alerter,
        outcome_tracker=tracker,
        _clock=lambda: fake_time[0],
        _poll_interval=10.0,
    )
    engine._fake_sleep = fake_sleep  # type: ignore[attr-defined]

    await _fire_alert(engine, _bullish_event(), _bullish_cls())

    alerter.alert.assert_not_called()
    assert store.list_open() == []
    assert store.list_closed() == []


async def test_theoretical_fill_model_fills_at_entry_immediately(tmp_path: Path) -> None:
    """
    With fill_model=theoretical, fill_price is set at alert entry price
    immediately and persisted; first tick above entry still hits target.
    """
    from oilbot.config import Config

    alerter = AsyncMock()
    db_path = tmp_path / "v3_theo.db"
    db = Database(db_path)
    store = OutcomesStore(db_path)

    theo_config = Config(
        telegram_token="tok",
        telegram_chat_id="123",
        outcome_timeout_minutes=60,
        fill_model="theoretical",
        stop_slippage_ticks=0,
        tick_size=0.01,
        outcome_telegram=False,
    )
    tracker = OutcomeTracker(theo_config, db_path)

    fake_time = [T0]

    async def fake_sleep(secs: float) -> None:
        fake_time[0] += timedelta(seconds=secs)

    engine = StrategyEngine(
        _engine_config(),  # type: ignore[arg-type]
        _FakeMonitor(_TRIGGER_BARS),  # type: ignore[arg-type]
        db,
        alerter=alerter,
        outcome_tracker=tracker,
        _clock=lambda: fake_time[0],
        _poll_interval=10.0,
    )
    engine._fake_sleep = fake_sleep  # type: ignore[attr-defined]

    await _fire_alert(engine, _bullish_event(), _bullish_cls())
    alerter.alert.assert_called_once()

    # Theoretical fill: fill_price should already be set at entry (100.05)
    alert_id = list(tracker._open.keys())[0]
    assert tracker._open[alert_id].fill_price == pytest.approx(100.05)

    # Hit target
    now = datetime.now(UTC)
    await tracker._process_tick(100.35, now)

    closed = store.list_closed()
    assert len(closed) == 1
    assert closed[0]["status"] == "target_hit"
    assert closed[0]["realized_r"] == pytest.approx(1.5, abs=0.01)
