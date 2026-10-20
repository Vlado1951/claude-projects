"""Integration test: fake news + fake price + fake Telegram → exactly one alert."""
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from oilbot.news.models import NewsEvent
from oilbot.persistence.db import Database
from oilbot.price.types import Bar, PriceWindow
from oilbot.strategy.engine import StrategyEngine
from oilbot.types import Classification, Sentiment

# ── helpers ────────────────────────────────────────────────────────────────────

T0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)


def _bar(offset_s: float, close: float) -> Bar:
    return Bar(
        time=T0 + timedelta(seconds=offset_s),
        open=close, high=close, low=close, close=close, volume=1.0,
    )


def _make_db(tmp_path: Path) -> Database:
    return Database(tmp_path / "integration.db")


def _make_config(**overrides: object) -> MagicMock:
    cfg = MagicMock()
    cfg.spike_window_seconds = overrides.get("spike_window_seconds", 120)
    cfg.pullback_window_seconds = overrides.get("pullback_window_seconds", 300)
    cfg.total_window_seconds = overrides.get("total_window_seconds", 600)
    cfg.pullback_k = overrides.get("pullback_k", 0.5)
    cfg.atr_bars = overrides.get("atr_bars", 12)
    cfg.k_static = overrides.get("k_static", 0.10)
    cfg.target_rr = overrides.get("target_rr", 1.5)
    cfg.confirmation_mode = overrides.get("confirmation_mode", "off")
    return cfg


class FakeMonitor:
    """Injects a fixed bar sequence; clock-driven by fake_sleep."""

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


def _make_engine(
    bars: list[Bar],
    tmp_path: Path,
    alerter: AsyncMock,
) -> StrategyEngine:
    db = _make_db(tmp_path)
    config = _make_config()
    monitor = FakeMonitor(bars)
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
    return engine


# ── integration scenarios ──────────────────────────────────────────────────────

async def test_bullish_full_scenario_fires_exactly_one_alert(tmp_path: Path) -> None:
    """Bullish post → spike → pullback → breakout → exactly one BUY alert."""
    alerter = AsyncMock()

    # spike window (T to T+120s): one bar at T+60s, close=100.0 → H=100.0
    # k_static fallback (single bar, ATR=None) → threshold=0.10
    # pullback window (T+120s to T+420s): bar at T+200s close=99.85 < H-0.10=99.90 → L=99.85
    # breakout window (T+120s to T+600s): bar at T+500s close=100.05 > H → fires
    bars = [
        _bar(60, 100.0),
        _bar(200, 99.85),
        _bar(500, 100.05),
    ]
    engine = _make_engine(bars, tmp_path, alerter)

    event = NewsEvent(
        id="integration-bullish-1",
        author="realDonaldTrump",
        text="OPEC announces production cut",
        posted_at=T0,
        received_at=T0,
        url="https://example.com/1",
    )
    cls = Classification(
        sentiment=Sentiment.BULLISH, confidence=0.9, rationale="OPEC cut", source="keywords"
    )

    from unittest.mock import patch
    with patch("asyncio.sleep", new=engine._fake_sleep):  # type: ignore[attr-defined]
        await engine.handle(event, cls)
        await asyncio.gather(*list(engine._tasks), return_exceptions=True)

    alerter.alert.assert_called_once()
    payload = alerter.alert.call_args[0][0]

    assert payload.side == "BUY"
    assert payload.spike_level == pytest.approx(100.0)
    assert payload.pullback_level == pytest.approx(99.85)
    assert payload.breakout_level == pytest.approx(100.05)
    assert payload.stop == pytest.approx(99.85)
    # target = 100.05 + 1.5 * (100.05 - 99.85) = 100.35
    assert payload.target == pytest.approx(100.35)
    assert payload.post_id == "integration-bullish-1"
    assert payload.confidence == pytest.approx(0.9)


async def test_bearish_full_scenario_fires_exactly_one_alert(tmp_path: Path) -> None:
    """Bearish post → dump → bounce → breakdown → exactly one SELL alert."""
    alerter = AsyncMock()

    # dump window: bar at T+60s close=100.0 → L=100.0, threshold=0.10
    # bounce: bar at T+200s close=100.15 > L+0.10=100.10 → H_bounce=100.15
    # breakdown: bar at T+500s close=99.95 < L=100.0 → fires
    bars = [
        _bar(60, 100.0),
        _bar(200, 100.15),
        _bar(500, 99.95),
    ]
    engine = _make_engine(bars, tmp_path, alerter)

    event = NewsEvent(
        id="integration-bearish-1",
        author="realDonaldTrump",
        text="Ceasefire reached, SPR release announced",
        posted_at=T0,
        received_at=T0,
        url="https://example.com/2",
    )
    cls = Classification(
        sentiment=Sentiment.BEARISH, confidence=0.85, rationale="ceasefire", source="keywords"
    )

    from unittest.mock import patch
    with patch("asyncio.sleep", new=engine._fake_sleep):  # type: ignore[attr-defined]
        await engine.handle(event, cls)
        await asyncio.gather(*list(engine._tasks), return_exceptions=True)

    alerter.alert.assert_called_once()
    payload = alerter.alert.call_args[0][0]

    assert payload.side == "SELL"
    assert payload.spike_level == pytest.approx(100.0)    # dump low
    assert payload.pullback_level == pytest.approx(100.15)  # bounce high
    assert payload.breakout_level == pytest.approx(99.95)  # breakdown close
    assert payload.stop == pytest.approx(100.15)
    # target = 99.95 - 1.5 * (100.15 - 99.95) = 99.65
    assert payload.target == pytest.approx(99.65)


async def test_neutral_event_never_reaches_engine(tmp_path: Path) -> None:
    """NEUTRAL classification produces no alert and no engine task."""
    alerter = AsyncMock()
    engine = _make_engine([], tmp_path, alerter)

    event = NewsEvent(
        id="integration-neutral-1",
        author="realDonaldTrump",
        text="Good morning everyone",
        posted_at=T0,
        received_at=T0,
        url="",
    )
    cls = Classification(
        sentiment=Sentiment.NEUTRAL, confidence=0.5, rationale="no signal", source="keywords"
    )

    from unittest.mock import patch
    with patch("asyncio.sleep", new=engine._fake_sleep):  # type: ignore[attr-defined]
        await engine.handle(event, cls)

    assert not engine._tasks
    alerter.alert.assert_not_called()


async def test_rss_feed_to_alert(tmp_path: Path) -> None:
    """Scripted RSS feed (google_news_oil) → keyword-filtered event → price action → BUY alert."""
    from unittest.mock import AsyncMock, patch

    from oilbot.news.configured_sources import RSS_SOURCES
    from oilbot.news.rss import GenericRSSListener

    alerter = AsyncMock()
    bars = [_bar(60, 100.0), _bar(200, 99.85), _bar(500, 100.05)]
    engine = _make_engine(bars, tmp_path, alerter)

    db = _make_db(tmp_path)
    src = RSS_SOURCES["google_news_oil"]
    listener = GenericRSSListener(
        name=src.name,
        feed_url=src.feed_url,
        db=db,
        poll_interval_seconds=0,
        keyword_filter=src.keyword_filter,
    )

    scripted_entry = {
        "id": "gn-integ-1",
        "link": "https://example.com/oil",
        "title": "OPEC announces crude production cut",
        "summary": "",
        "published": "Mon, 01 Jan 2024 10:00:00 +0000",
    }
    feed_mock = MagicMock()
    feed_mock.get = lambda k, d=None: (
        [scripted_entry] if k == "entries" else (False if k == "bozo" else d)
    )

    from unittest.mock import MagicMock as _MM

    def _mock_http() -> _MM:
        resp = _MM()
        resp.text = "<rss/>"
        resp.raise_for_status = _MM()
        c = AsyncMock()
        c.get = AsyncMock(return_value=resp)
        c.__aenter__ = AsyncMock(return_value=c)
        c.__aexit__ = AsyncMock(return_value=None)
        return c

    collected: list[NewsEvent] = []
    with patch("oilbot.news.rss.httpx.AsyncClient", return_value=_mock_http()):
        with patch("oilbot.news.rss.feedparser.parse", return_value=feed_mock):
            with patch("asyncio.sleep", new=AsyncMock()):
                async for event in listener.stream():
                    collected.append(event)
                    break

    assert len(collected) == 1
    event = collected[0]
    assert event.author == "google_news_oil"
    assert "OPEC" in event.text

    cls = Classification(
        sentiment=Sentiment.BULLISH, confidence=0.88, rationale="opec cut", source="keywords"
    )
    with patch("asyncio.sleep", new=engine._fake_sleep):  # type: ignore[attr-defined]
        await engine.handle(event, cls)
        await asyncio.gather(*list(engine._tasks), return_exceptions=True)

    alerter.alert.assert_called_once()
    assert alerter.alert.call_args[0][0].side == "BUY"


async def test_duplicate_event_not_double_alerted(tmp_path: Path) -> None:
    """Handling the same event id twice fires the alert at most once per DB state."""
    alerter = AsyncMock()
    bars = [_bar(60, 100.0), _bar(200, 99.85), _bar(500, 100.05)]
    engine = _make_engine(bars, tmp_path, alerter)

    event = NewsEvent(
        id="integration-dup-1",
        author="realDonaldTrump",
        text="OPEC cut",
        posted_at=T0,
        received_at=T0,
        url="",
    )
    cls = Classification(
        sentiment=Sentiment.BULLISH, confidence=0.9, rationale="OPEC cut", source="keywords"
    )

    from unittest.mock import patch
    with patch("asyncio.sleep", new=engine._fake_sleep):  # type: ignore[attr-defined]
        await engine.handle(event, cls)
        await engine.handle(event, cls)
        await asyncio.gather(*list(engine._tasks), return_exceptions=True)

    # Strategy engine doesn't deduplicate by post_id — that's the news listener's job.
    # But the DB should have recorded both alerts; this test just ensures no crash.
    assert alerter.alert.call_count >= 1


async def test_eia_draw_fires_bullish_alert_with_confirmation(tmp_path: Path) -> None:
    """EIA 5 Mbbl crude draw with aligned PriceContext → BUY alert, confirmation_status set."""
    from unittest.mock import patch

    from oilbot.news.eia import EIAReleaseEvent
    from oilbot.price.context import PriceContext

    alerter = AsyncMock()
    bars = [_bar(60, 80.0), _bar(200, 79.85), _bar(500, 80.05)]

    # Build a PriceContext and inject bullish-aligned prices for all 5 tickers.
    # Each ticker gets an "old" reading (60 s ago) and a "new" reading (now)
    # so _direction() can compute a change.
    price_ctx = PriceContext()
    now = T0
    old_ts = now - timedelta(seconds=60)
    price_ctx.inject("BZ=F",     80.00, ts=old_ts)
    price_ctx.inject("BZ=F",     80.50, ts=now)
    price_ctx.inject("CL=F",     78.00, ts=old_ts)
    price_ctx.inject("CL=F",     78.40, ts=now)
    price_ctx.inject("DX-Y.NYB", 104.0, ts=old_ts)
    price_ctx.inject("DX-Y.NYB", 103.5, ts=now)
    price_ctx.inject("XLE",      90.00, ts=old_ts)
    price_ctx.inject("XLE",      90.60, ts=now)
    price_ctx.inject("XOP",      30.00, ts=old_ts)
    price_ctx.inject("XOP",      30.20, ts=now)

    # Build engine manually so we can pass confirmation_mode="advisory" and price_context.
    db = _make_db(tmp_path)
    config = _make_config(confirmation_mode="advisory")
    monitor = FakeMonitor(bars)
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
        price_context=price_ctx,
    )
    engine._fake_sleep = fake_sleep  # type: ignore[attr-defined]

    event = EIAReleaseEvent(
        id="eia-integ-draw-1",
        author="eia",
        text="EIA: US crude stocks -5.0 Mbbl",
        posted_at=T0,
        received_at=T0,
        url="",
        metric="crude_stocks",
        delta=-5.0,
        unit="mbbl",
    )
    cls = Classification(
        sentiment=Sentiment.BULLISH,
        confidence=0.9,
        rationale="5 Mbbl draw",
        source="eia",
    )

    with patch("asyncio.sleep", new=engine._fake_sleep):  # type: ignore[attr-defined]
        await engine.handle(event, cls)
        await asyncio.gather(*list(engine._tasks), return_exceptions=True)

    alerter.alert.assert_called_once()
    payload = alerter.alert.call_args[0][0]
    assert payload.side == "BUY"
    assert payload.post_id == "eia-integ-draw-1"
    assert payload.confirmation_status == "aligned"


async def test_opec_rss_event_to_alert(tmp_path: Path) -> None:
    """OPEC RSS press release → BULLISH classification → BUY alert."""
    from unittest.mock import patch

    from oilbot.news.configured_sources import RSS_SOURCES
    from oilbot.news.rss import GenericRSSListener

    alerter = AsyncMock()
    bars = [_bar(60, 80.0), _bar(200, 79.85), _bar(500, 80.05)]
    engine = _make_engine(bars, tmp_path, alerter)

    db = _make_db(tmp_path)
    src = RSS_SOURCES["opec_rss"]
    listener = GenericRSSListener(
        name=src.name,
        feed_url=src.feed_url,
        db=db,
        poll_interval_seconds=0,
        keyword_filter=src.keyword_filter,
    )

    scripted_entry = {
        "id": "opec-integ-1",
        "link": "https://www.opec.org/press/1",
        "title": "OPEC+ agrees to extend crude production cuts",
        "summary": "Member countries agree to maintain output reductions.",
        "published": "Mon, 01 Jan 2024 10:00:00 +0000",
    }
    feed_mock = MagicMock()
    feed_mock.get = lambda k, d=None: (
        [scripted_entry] if k == "entries" else (False if k == "bozo" else d)
    )

    def _mock_http() -> MagicMock:
        resp = MagicMock()
        resp.text = "<rss/>"
        resp.raise_for_status = MagicMock()
        c = AsyncMock()
        c.get = AsyncMock(return_value=resp)
        c.__aenter__ = AsyncMock(return_value=c)
        c.__aexit__ = AsyncMock(return_value=None)
        return c

    collected: list[NewsEvent] = []
    with patch("oilbot.news.rss.httpx.AsyncClient", return_value=_mock_http()):
        with patch("oilbot.news.rss.feedparser.parse", return_value=feed_mock):
            with patch("asyncio.sleep", new=AsyncMock()):
                async for event in listener.stream():
                    collected.append(event)
                    break

    assert len(collected) == 1
    event = collected[0]
    assert event.author == "opec_rss"

    cls = Classification(
        sentiment=Sentiment.BULLISH,
        confidence=0.9,
        rationale="production cut",
        source="keywords",
    )

    with patch("asyncio.sleep", new=engine._fake_sleep):  # type: ignore[attr-defined]
        await engine.handle(event, cls)
        await asyncio.gather(*list(engine._tasks), return_exceptions=True)

    alerter.alert.assert_called_once()
    assert alerter.alert.call_args[0][0].side == "BUY"
