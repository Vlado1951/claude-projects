"""Tests for EIAReleaseListener and EIA classifier branch."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from oilbot.classifier.hybrid import HybridClassifier
from oilbot.news.eia import (
    EIADataset,
    EIAReleaseEvent,
    EIAReleaseListener,
    _next_release_time,
)
from oilbot.types import Sentiment

_ET = ZoneInfo("America/New_York")


def _make_dataset(**kwargs: object) -> EIADataset:
    defaults: dict[str, object] = dict(
        name="eia_petroleum",
        metric="crude_stocks",
        unit="mbbl",
        api_path="petroleum/stoc/wstk/data/",
        facets={"product": ["EPC0"], "duoarea": ["NUS"], "process": ["SAE"]},
        value_multiplier=0.001,
        weekday=2,
        release_hour_et=10,
        release_minute_et=30,
    )
    defaults.update(kwargs)
    return EIADataset(**defaults)  # type: ignore[arg-type]


def _listener(**kwargs: object) -> EIAReleaseListener:
    return EIAReleaseListener(api_key="test_key", dataset=_make_dataset(**kwargs))


def _eia_response(v0: float, v1: float, period: str = "2024-01-10") -> dict:
    return {
        "response": {
            "data": [
                {"period": period, "value": v0},
                {"period": "2024-01-03", "value": v1},
            ]
        }
    }


def _make_config() -> MagicMock:
    cfg = MagicMock()
    cfg.llm_provider = "none"
    cfg.llm_api_key = ""
    cfg.eia_crude_threshold_mbbl = 2.0
    cfg.eia_rig_threshold = 5
    return cfg


def _mock_http(body: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = body
    resp.raise_for_status = MagicMock()
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


# ── _next_release_time ────────────────────────────────────────────────────────


def test_next_release_time_before_window() -> None:
    """Tuesday → next release is Wednesday 10:30 ET."""
    ds = _make_dataset()
    now = datetime(2024, 1, 9, 14, 0, tzinfo=UTC)  # Tuesday 09:00 ET
    release = _next_release_time(ds, now)
    release_et = release.astimezone(_ET)
    assert release_et.weekday() == 2
    assert release_et.hour == 10
    assert release_et.minute == 30


def test_next_release_time_during_window() -> None:
    """Wednesday 10:40 ET (in window) → same week's release."""
    ds = _make_dataset()
    now = datetime(2024, 1, 10, 15, 40, tzinfo=UTC)  # Wed 10:40 ET
    release = _next_release_time(ds, now)
    release_et = release.astimezone(_ET)
    now_et = now.astimezone(_ET)
    assert release_et.weekday() == 2
    assert (release_et.date() - now_et.date()).days == 0


def test_next_release_time_past_deadline() -> None:
    """Wednesday 11:30 ET (past 30-min window) → next week."""
    ds = _make_dataset()
    now = datetime(2024, 1, 10, 16, 30, tzinfo=UTC)  # Wed 11:30 ET
    release = _next_release_time(ds, now)
    release_et = release.astimezone(_ET)
    now_et = now.astimezone(_ET)
    assert (release_et.date() - now_et.date()).days == 7


# ── fetch_latest ──────────────────────────────────────────────────────────────


async def test_fetch_latest_computes_draw() -> None:
    """Stocks fell 5000 kbbl → delta = -5.0 mbbl."""
    listener = _listener()
    with patch("oilbot.news.eia.httpx.AsyncClient",
               return_value=_mock_http(_eia_response(430_000, 435_000))):
        result = await listener.fetch_latest()

    assert result is not None
    period, delta = result
    assert period == "2024-01-10"
    assert delta == pytest.approx(-5.0)


async def test_fetch_latest_computes_build() -> None:
    """Stocks rose 3000 kbbl → delta = +3.0 mbbl."""
    listener = _listener()
    with patch("oilbot.news.eia.httpx.AsyncClient",
               return_value=_mock_http(_eia_response(438_000, 435_000))):
        result = await listener.fetch_latest()

    assert result is not None
    _, delta = result
    assert delta == pytest.approx(3.0)


async def test_fetch_latest_returns_none_on_insufficient_rows() -> None:
    body = {"response": {"data": [{"period": "2024-01-10", "value": 430_000}]}}
    listener = _listener()
    with patch("oilbot.news.eia.httpx.AsyncClient", return_value=_mock_http(body)):
        result = await listener.fetch_latest()
    assert result is None


async def test_fetch_latest_raises_on_http_error() -> None:
    listener = _listener()
    client = AsyncMock()
    client.get = AsyncMock(side_effect=OSError("network error"))
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    with patch("oilbot.news.eia.httpx.AsyncClient", return_value=client):
        with pytest.raises(OSError):
            await listener.fetch_latest()


# ── make_event ────────────────────────────────────────────────────────────────


def test_make_event_draw() -> None:
    listener = _listener()
    event = listener.make_event("2024-01-10", -5.0)
    assert isinstance(event, EIAReleaseEvent)
    assert event.delta == pytest.approx(-5.0)
    assert event.metric == "crude_stocks"
    assert event.unit == "mbbl"
    assert "draw" in event.text.lower()
    assert event.id == "eia_petroleum_2024-01-10"


def test_make_event_build() -> None:
    listener = _listener()
    event = listener.make_event("2024-01-10", 3.0)
    assert "build" in event.text.lower()


# ── stream ────────────────────────────────────────────────────────────────────


async def test_stream_emits_event_on_new_period() -> None:
    """Stream emits one event when new period data arrives in the window."""
    listener = _listener()

    # Wednesday 10:35 ET → inside the polling window
    fake_now = datetime(2024, 1, 10, 10, 35, tzinfo=_ET).astimezone(UTC)

    async def fake_fetch() -> tuple[str, float]:
        return "2024-01-10", -5.0

    collected: list[EIAReleaseEvent] = []
    with patch("oilbot.news.eia.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        with patch.object(listener, "fetch_latest", new=AsyncMock(side_effect=fake_fetch)):
            with patch("oilbot.news.eia.asyncio.sleep", new=AsyncMock()):
                async for event in listener.stream():
                    collected.append(event)
                    break

    assert len(collected) == 1
    assert collected[0].delta == pytest.approx(-5.0)


async def test_stream_does_not_re_emit_same_period() -> None:
    """Already-seen period is not re-emitted."""
    listener = _listener()
    listener._last_period = "2024-01-10"

    call_count = 0

    async def fake_fetch() -> tuple[str, float]:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            return "2024-01-10", -5.0
        raise asyncio.CancelledError

    fake_now = datetime(2024, 1, 10, 10, 35, tzinfo=_ET).astimezone(UTC)
    collected: list[EIAReleaseEvent] = []

    with patch("oilbot.news.eia.datetime") as mock_dt:
        mock_dt.now.return_value = fake_now
        with patch.object(listener, "fetch_latest", new=AsyncMock(side_effect=fake_fetch)):
            with patch("oilbot.news.eia.asyncio.sleep", new=AsyncMock()):
                with pytest.raises(asyncio.CancelledError):
                    async for event in listener.stream():
                        collected.append(event)

    assert len(collected) == 0


# ── classifier EIA branch ─────────────────────────────────────────────────────


async def test_classifier_bullish_draw() -> None:
    """Draw > threshold → BULLISH."""
    clf = HybridClassifier(_make_config())
    event = _listener().make_event("2024-01-10", -5.0)
    cls = await clf.classify(event)
    assert cls.sentiment == Sentiment.BULLISH
    assert cls.source == "eia"
    assert cls.confidence > 0.5


async def test_classifier_bearish_build() -> None:
    """Build > threshold → BEARISH."""
    clf = HybridClassifier(_make_config())
    event = _listener().make_event("2024-01-10", 3.0)
    cls = await clf.classify(event)
    assert cls.sentiment == Sentiment.BEARISH
    assert cls.source == "eia"


async def test_classifier_neutral_below_threshold() -> None:
    """Change < threshold → NEUTRAL."""
    clf = HybridClassifier(_make_config())
    event = _listener().make_event("2024-01-10", -1.0)
    cls = await clf.classify(event)
    assert cls.sentiment == Sentiment.NEUTRAL
    assert cls.source == "eia"


async def test_classifier_rig_count_uses_rig_threshold() -> None:
    """8-rig drop with threshold=5 → BULLISH."""
    rig_ds = _make_dataset(
        name="eia_rigs", metric="oil_rig_count", unit="rigs",
        weekday=4, release_hour_et=13, release_minute_et=0, value_multiplier=1.0,
    )
    rig_listener = EIAReleaseListener(api_key="key", dataset=rig_ds)
    event = rig_listener.make_event("2024-01-12", -8.0)
    cls = await HybridClassifier(_make_config()).classify(event)
    assert cls.sentiment == Sentiment.BULLISH


async def test_classifier_rig_count_neutral_small_change() -> None:
    """3-rig change below threshold=5 → NEUTRAL."""
    rig_ds = _make_dataset(
        name="eia_rigs", metric="oil_rig_count", unit="rigs",
        weekday=4, release_hour_et=13, release_minute_et=0, value_multiplier=1.0,
    )
    rig_listener = EIAReleaseListener(api_key="key", dataset=rig_ds)
    event = rig_listener.make_event("2024-01-12", -3.0)
    cls = await HybridClassifier(_make_config()).classify(event)
    assert cls.sentiment == Sentiment.NEUTRAL
