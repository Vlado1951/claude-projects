"""Unit tests for OutcomeTracker (Phase 19)."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from oilbot.config import Config
from oilbot.outcomes.tracker import OutcomeTracker
from oilbot.persistence.db import Database
from oilbot.persistence.outcomes import OutcomesStore
from oilbot.types import AlertPayload, Sentiment

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(
    timeout_minutes: int = 5,
    fill_model: str = "next_tick",
    slippage: int = 0,
    tick_size: float = 0.01,
    outcome_telegram: bool = False,
) -> Config:
    return Config(
        telegram_token="tok",
        telegram_chat_id="123",
        outcome_timeout_minutes=timeout_minutes,
        fill_model=fill_model,  # type: ignore[arg-type]
        stop_slippage_ticks=slippage,
        tick_size=tick_size,
        outcome_telegram=outcome_telegram,
    )


def _tracker(tmp_path: Path, **kwargs: Any) -> OutcomeTracker:
    return OutcomeTracker(_config(**kwargs), tmp_path / "test.db")


def _buy_payload(alert_id: int = 1) -> AlertPayload:
    """BUY: entry=80, stop=79, target=81.5  →  risk=1, target_r=1.5"""
    return AlertPayload(
        side="BUY",
        post_id=f"post-{alert_id}",
        post_text="test",
        sentiment=Sentiment.BULLISH,
        confidence=1.0,
        spike_level=81.0,
        pullback_level=79.0,
        breakout_level=80.0,
        breakout_at=datetime.now(UTC),
        stop=79.0,
        target=81.5,
    )


def _sell_payload(alert_id: int = 1) -> AlertPayload:
    """SELL: entry=80, stop=81, target=78.5  →  risk=1, target_r=1.5"""
    return AlertPayload(
        side="SELL",
        post_id=f"post-{alert_id}",
        post_text="test",
        sentiment=Sentiment.BEARISH,
        confidence=1.0,
        spike_level=79.0,
        pullback_level=81.0,
        breakout_level=80.0,
        breakout_at=datetime.now(UTC),
        stop=81.0,
        target=78.5,
    )


def _alert_id(tmp_path: Path, post_id: str = "post-1", side: str = "BUY") -> int:
    db = Database(tmp_path / "test.db")
    if side == "BUY":
        return db.record_alert(post_id, "BUY", 80.0, 79.0, 81.5)
    return db.record_alert(post_id, "SELL", 80.0, 81.0, 78.5)


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# BUY — target hit
# ---------------------------------------------------------------------------


async def test_buy_target_hit(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    aid = _alert_id(tmp_path)
    OutcomesStore(tmp_path / "test.db").open_outcome(aid)

    t.open_alert(aid, _buy_payload(aid))

    now = _now()
    await t._process_tick(80.01, now)   # fill at 80.01 (>= entry 80)
    await t._process_tick(80.80, now)   # MFE update
    await t._process_tick(81.50, now)   # hit target

    row = OutcomesStore(tmp_path / "test.db").get_by_alert(aid)
    assert row is not None
    assert row["status"] == "target_hit"
    assert row["realized_r"] == pytest.approx(1.5, abs=0.01)
    assert row["exit_price"] == pytest.approx(81.5)


# ---------------------------------------------------------------------------
# BUY — stop hit
# ---------------------------------------------------------------------------


async def test_buy_stop_hit(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    aid = _alert_id(tmp_path)
    OutcomesStore(tmp_path / "test.db").open_outcome(aid)

    t.open_alert(aid, _buy_payload(aid))

    now = _now()
    await t._process_tick(80.01, now)
    await t._process_tick(79.00, now)   # hit stop exactly

    row = OutcomesStore(tmp_path / "test.db").get_by_alert(aid)
    assert row is not None
    assert row["status"] == "stop_hit"
    assert row["realized_r"] == pytest.approx(-1.0, abs=0.01)


# ---------------------------------------------------------------------------
# BUY — timeout (never filled)
# ---------------------------------------------------------------------------


async def test_buy_timeout_never_filled(tmp_path: Path) -> None:
    t = _tracker(tmp_path, timeout_minutes=1)
    aid = _alert_id(tmp_path)
    OutcomesStore(tmp_path / "test.db").open_outcome(aid)

    t.open_alert(aid, _buy_payload(aid))
    # Backdate tracked_from so timeout fires
    t._open[aid].tracked_from = _now() - timedelta(minutes=2)

    await t._check_timeouts(_now())

    row = OutcomesStore(tmp_path / "test.db").get_by_alert(aid)
    assert row is not None
    assert row["status"] == "timeout"
    assert row["realized_r"] is None   # never filled → no R


# ---------------------------------------------------------------------------
# BUY — timeout (filled, uses last tick price for R)
# ---------------------------------------------------------------------------


async def test_buy_timeout_filled_uses_last_price(tmp_path: Path) -> None:
    t = _tracker(tmp_path, timeout_minutes=1)
    aid = _alert_id(tmp_path)
    OutcomesStore(tmp_path / "test.db").open_outcome(aid)

    t.open_alert(aid, _buy_payload(aid))
    t._last_tick_price = 80.50

    now = _now()
    await t._process_tick(80.01, now)  # fill
    t._open[aid].tracked_from = now - timedelta(minutes=2)

    await t._check_timeouts(_now())

    row = OutcomesStore(tmp_path / "test.db").get_by_alert(aid)
    assert row is not None
    assert row["status"] == "timeout"
    # R = (80.50 - 80.0) / 1.0 = 0.5
    assert row["realized_r"] == pytest.approx(0.5, abs=0.01)


# ---------------------------------------------------------------------------
# SELL — target hit
# ---------------------------------------------------------------------------


async def test_sell_target_hit(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    aid = _alert_id(tmp_path, "post-1", "SELL")
    OutcomesStore(tmp_path / "test.db").open_outcome(aid)

    t.open_alert(aid, _sell_payload(aid))

    now = _now()
    await t._process_tick(79.99, now)   # fill (<= entry 80)
    await t._process_tick(79.20, now)
    await t._process_tick(78.50, now)   # hit target

    row = OutcomesStore(tmp_path / "test.db").get_by_alert(aid)
    assert row is not None
    assert row["status"] == "target_hit"
    assert row["realized_r"] == pytest.approx(1.5, abs=0.01)


# ---------------------------------------------------------------------------
# SELL — stop hit
# ---------------------------------------------------------------------------


async def test_sell_stop_hit(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    aid = _alert_id(tmp_path, "post-1", "SELL")
    OutcomesStore(tmp_path / "test.db").open_outcome(aid)

    t.open_alert(aid, _sell_payload(aid))

    now = _now()
    await t._process_tick(79.99, now)   # fill
    await t._process_tick(81.00, now)   # hit stop

    row = OutcomesStore(tmp_path / "test.db").get_by_alert(aid)
    assert row is not None
    assert row["status"] == "stop_hit"
    assert row["realized_r"] == pytest.approx(-1.0, abs=0.01)


# ---------------------------------------------------------------------------
# next_tick vs theoretical fill model
# ---------------------------------------------------------------------------


async def test_next_tick_fill_model_waits_for_price(tmp_path: Path) -> None:
    """next_tick: fill only when price crosses entry; pre-entry ticks are ignored."""
    t = _tracker(tmp_path, fill_model="next_tick")
    aid = _alert_id(tmp_path)
    OutcomesStore(tmp_path / "test.db").open_outcome(aid)

    t.open_alert(aid, _buy_payload(aid))
    now = _now()

    await t._process_tick(79.50, now)   # below entry — not filled yet
    assert t._open[aid].fill_price is None

    await t._process_tick(80.05, now)   # crosses entry — filled
    assert t._open[aid].fill_price == pytest.approx(80.05)


async def test_theoretical_fill_model_fills_immediately(tmp_path: Path) -> None:
    """theoretical: fill_price set to entry at open_alert time."""
    t = _tracker(tmp_path, fill_model="theoretical")
    aid = _alert_id(tmp_path)
    OutcomesStore(tmp_path / "test.db").open_outcome(aid)

    t.open_alert(aid, _buy_payload(aid))
    assert t._open[aid].fill_price == pytest.approx(80.0)

    # DB should have fill_price written immediately
    row = OutcomesStore(tmp_path / "test.db").get_by_alert(aid)
    assert row is not None
    assert row["fill_price"] == pytest.approx(80.0)


async def test_theoretical_and_next_tick_produce_different_fill_prices(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "test.db")
    store = OutcomesStore(tmp_path / "test.db")

    aid_next = db.record_alert("p1", "BUY", 80.0, 79.0, 81.5)
    aid_theo = db.record_alert("p2", "BUY", 80.0, 79.0, 81.5)
    store.open_outcome(aid_next)
    store.open_outcome(aid_theo)

    tracker_next = OutcomeTracker(_config(fill_model="next_tick"), tmp_path / "test.db")
    tracker_theo = OutcomeTracker(_config(fill_model="theoretical"), tmp_path / "test.db")

    tracker_next.open_alert(aid_next, _buy_payload(aid_next))
    tracker_theo.open_alert(aid_theo, _buy_payload(aid_theo))

    now = _now()
    # First tick at 80.10 — next_tick fills at 80.10, theoretical already at 80.0
    await tracker_next._process_tick(80.10, now)
    await tracker_theo._process_tick(80.10, now)

    assert tracker_next._open[aid_next].fill_price == pytest.approx(80.10)
    assert tracker_theo._open[aid_theo].fill_price == pytest.approx(80.0)


# ---------------------------------------------------------------------------
# Stop slippage
# ---------------------------------------------------------------------------


async def test_stop_slippage_worsens_realized_r(tmp_path: Path) -> None:
    """With 1-tick slippage, stop_hit exit is worse than stated stop."""
    t = _tracker(tmp_path, slippage=1, tick_size=0.01)
    aid = _alert_id(tmp_path)
    OutcomesStore(tmp_path / "test.db").open_outcome(aid)

    t.open_alert(aid, _buy_payload(aid))
    now = _now()
    await t._process_tick(80.01, now)   # fill
    await t._process_tick(79.00, now)   # stop hit, exit at 79.0 - 0.01 = 78.99

    row = OutcomesStore(tmp_path / "test.db").get_by_alert(aid)
    assert row is not None
    assert row["status"] == "stop_hit"
    # R = (78.99 - 80.0) / 1.0 = -1.01
    assert row["realized_r"] == pytest.approx(-1.01, abs=0.001)
    assert row["realized_r"] < -1.0   # worse than no-slippage


# ---------------------------------------------------------------------------
# MFE / MAE
# ---------------------------------------------------------------------------


async def test_mfe_mae_tracked_correctly(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    aid = _alert_id(tmp_path)
    OutcomesStore(tmp_path / "test.db").open_outcome(aid)

    t.open_alert(aid, _buy_payload(aid))
    now = _now()
    await t._process_tick(80.01, now)   # fill
    await t._process_tick(80.80, now)   # MFE candidate
    await t._process_tick(80.30, now)   # pullback — MAE candidate
    await t._process_tick(80.50, now)
    await t._process_tick(81.50, now)   # target hit, MFE = 81.5

    row = OutcomesStore(tmp_path / "test.db").get_by_alert(aid)
    assert row is not None
    assert row["mfe_price"] == pytest.approx(81.5)
    assert row["mae_price"] == pytest.approx(80.01)   # lowest observed after fill
    assert row["mfe_r"] == pytest.approx(1.5, abs=0.01)
    # mae_r = (80.01 - 80.0) / 1.0 = 0.01
    assert row["mae_r"] == pytest.approx(0.01, abs=0.001)


async def test_mae_not_tracked_before_fill(tmp_path: Path) -> None:
    """Adverse ticks before fill (next_tick mode) must not affect MAE."""
    t = _tracker(tmp_path, fill_model="next_tick")
    aid = _alert_id(tmp_path)
    OutcomesStore(tmp_path / "test.db").open_outcome(aid)

    t.open_alert(aid, _buy_payload(aid))
    now = _now()
    await t._process_tick(79.50, now)   # below entry — not yet filled, ignore
    await t._process_tick(80.01, now)   # fill
    await t._process_tick(81.50, now)   # target

    row = OutcomesStore(tmp_path / "test.db").get_by_alert(aid)
    assert row is not None
    # MAE should be 80.01, NOT 79.50
    assert row["mae_price"] == pytest.approx(80.01, abs=0.01)


# ---------------------------------------------------------------------------
# Disconnect / reconnect — paused_seconds
# ---------------------------------------------------------------------------


def test_disconnect_sets_paused_since(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    aid = _alert_id(tmp_path)
    OutcomesStore(tmp_path / "test.db").open_outcome(aid)
    t.open_alert(aid, _buy_payload(aid))

    t.handle_disconnect()
    assert t._paused
    assert t._open[aid].paused_since is not None


def test_reconnect_accumulates_paused_seconds(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    aid = _alert_id(tmp_path)
    OutcomesStore(tmp_path / "test.db").open_outcome(aid)
    t.open_alert(aid, _buy_payload(aid))

    pause_time = _now()
    t._open[aid].paused_since = pause_time - timedelta(seconds=30)
    t._paused = True

    t.handle_reconnect()

    assert not t._paused
    assert t._open[aid].paused_seconds >= 30
    assert t._open[aid].paused_since is None


def test_paused_seconds_extends_effective_tracking_window(tmp_path: Path) -> None:
    """Timeout should not fire while tracker has been paused for the full window."""
    t = _tracker(tmp_path, timeout_minutes=1)
    aid = _alert_id(tmp_path)
    OutcomesStore(tmp_path / "test.db").open_outcome(aid)
    t.open_alert(aid, _buy_payload(aid))

    # Alert started 70s ago, but 60s of that was paused — effective = 10s
    t._open[aid].tracked_from = _now() - timedelta(seconds=70)
    t._open[aid].paused_seconds = 60

    asyncio.get_event_loop().run_until_complete(t._check_timeouts(_now()))

    # Should still be tracking
    assert aid in t._open


# ---------------------------------------------------------------------------
# Restart recovery — resume from DB
# ---------------------------------------------------------------------------


async def test_resume_from_db_restores_open_alert(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    store = OutcomesStore(tmp_path / "test.db")

    aid = db.record_alert("post-r1", "BUY", 80.0, 79.0, 81.5)
    store.open_outcome(aid, tracked_from_at=_now())

    t = _tracker(tmp_path)
    await t._resume_from_db()

    assert aid in t._open
    assert t._open[aid].side == "BUY"
    assert t._open[aid].entry == pytest.approx(80.0)


async def test_resume_from_db_force_closes_stale_rows(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    store = OutcomesStore(tmp_path / "test.db")

    stale_from = _now() - timedelta(minutes=15)
    aid = db.record_alert("post-stale", "BUY", 80.0, 79.0, 81.5)
    store.open_outcome(aid, tracked_from_at=stale_from)

    # timeout_minutes=5 → stale cutoff = 10 min; row is 15 min old
    t = _tracker(tmp_path, timeout_minutes=5)
    await t._resume_from_db()

    assert aid not in t._open
    row = store.get_by_alert(aid)
    assert row is not None
    assert row["status"] == "timeout"


async def test_resume_from_db_preserves_fill_price(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    store = OutcomesStore(tmp_path / "test.db")

    aid = db.record_alert("post-fill", "BUY", 80.0, 79.0, 81.5)
    store.open_outcome(aid, tracked_from_at=_now())
    store.update_outcome(aid, fill_price=80.05, fill_at=_now().isoformat())

    t = _tracker(tmp_path)
    await t._resume_from_db()

    assert t._open[aid].fill_price == pytest.approx(80.05)


# ---------------------------------------------------------------------------
# open_count
# ---------------------------------------------------------------------------


def test_open_count_reflects_in_memory_state(tmp_path: Path) -> None:
    t = _tracker(tmp_path)
    db = Database(tmp_path / "test.db")
    store = OutcomesStore(tmp_path / "test.db")

    aid1 = db.record_alert("p1", "BUY", 80.0, 79.0, 81.5)
    aid2 = db.record_alert("p2", "BUY", 80.0, 79.0, 81.5)
    store.open_outcome(aid1)
    store.open_outcome(aid2)

    t.open_alert(aid1, _buy_payload(aid1))
    t.open_alert(aid2, _buy_payload(aid2))
    assert t.open_count == 2

    asyncio.get_event_loop().run_until_complete(
        t._process_tick(81.50, _now())
    )
    assert t.open_count == 0  # both closed (both got filled at 81.5 > entry, then hit target)


# ---------------------------------------------------------------------------
# Telegram outcome message
# ---------------------------------------------------------------------------


async def test_outcome_telegram_sent_on_resolve(tmp_path: Path) -> None:
    alerter = MagicMock()
    alerter.system = AsyncMock()

    t = OutcomeTracker(
        _config(outcome_telegram=True),
        tmp_path / "test.db",
        alerter=alerter,
    )
    aid = _alert_id(tmp_path)
    OutcomesStore(tmp_path / "test.db").open_outcome(aid)
    t.open_alert(aid, _buy_payload(aid))

    now = _now()
    await t._process_tick(80.01, now)
    await t._process_tick(81.50, now)

    alerter.system.assert_awaited_once()
    msg = alerter.system.call_args[0][0]
    assert "target_hit" in msg
    assert "BUY" in msg


async def test_outcome_telegram_not_sent_when_disabled(tmp_path: Path) -> None:
    alerter = MagicMock()
    alerter.system = AsyncMock()

    t = OutcomeTracker(
        _config(outcome_telegram=False),
        tmp_path / "test.db",
        alerter=alerter,
    )
    aid = _alert_id(tmp_path)
    OutcomesStore(tmp_path / "test.db").open_outcome(aid)
    t.open_alert(aid, _buy_payload(aid))

    now = _now()
    await t._process_tick(80.01, now)
    await t._process_tick(81.50, now)

    alerter.system.assert_not_awaited()
