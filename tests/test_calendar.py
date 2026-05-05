"""Tests for CalendarListener (Phase 15)."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from oilbot.news.calendar import (
    CalendarEvent,
    CalendarListener,
    _first_friday,
    _parse_fomc_dates,
    nfp_dates,
    upcoming_events,
)
from oilbot.types import Sentiment

_ET = ZoneInfo("America/New_York")


# ── _first_friday ─────────────────────────────────────────────────────────────


def test_first_friday_january_2025() -> None:
    assert _first_friday(2025, 1) == date(2025, 1, 3)


def test_first_friday_february_2025() -> None:
    assert _first_friday(2025, 2) == date(2025, 2, 7)


def test_first_friday_is_always_friday() -> None:
    for month in range(1, 13):
        d = _first_friday(2025, month)
        assert d.weekday() == 4, f"month {month}: {d} is not Friday"


# ── nfp_dates ─────────────────────────────────────────────────────────────────


def test_nfp_dates_returns_first_fridays() -> None:
    dates = nfp_dates(date(2025, 1, 1), count=3)
    assert dates[0] == date(2025, 1, 3)
    assert dates[1] == date(2025, 2, 7)
    assert dates[2] == date(2025, 3, 7)


def test_nfp_dates_excludes_past() -> None:
    dates = nfp_dates(date(2025, 1, 10), count=3)
    # Jan 3 is in the past relative to Jan 10 → starts from Feb
    assert all(d >= date(2025, 1, 10) for d in dates)


# ── _parse_fomc_dates ─────────────────────────────────────────────────────────


def _fomc_html(year: int, month: str, day_range: str) -> str:
    """Minimal HTML matching the Fed calendar structure."""
    return (
        f'<h4><a id="0">{year} FOMC Meetings</a></h4>'
        f'<div class="fomc-meeting__month col-xs-5"><strong>{month}</strong></div>'
        f'<div class="fomc-meeting__date col-xs-4">{day_range}</div>'
    )


def test_parse_fomc_dates_single_day() -> None:
    html = _fomc_html(2025, "January", "29")
    dates = _parse_fomc_dates(html)
    assert date(2025, 1, 29) in dates


def test_parse_fomc_dates_range() -> None:
    html = _fomc_html(2025, "January", "28-29")
    dates = _parse_fomc_dates(html)
    assert date(2025, 1, 29) in dates
    assert date(2025, 1, 28) not in dates


def test_parse_fomc_dates_en_dash_range() -> None:
    html = _fomc_html(2025, "March", "18–19")
    dates = _parse_fomc_dates(html)
    assert date(2025, 3, 19) in dates


def test_parse_fomc_dates_asterisk_stripped() -> None:
    """Dates with trailing asterisk (projected) still parse correctly."""
    html = _fomc_html(2026, "June", "16-17*")
    dates = _parse_fomc_dates(html)
    assert date(2026, 6, 17) in dates


def test_parse_fomc_dates_multiple() -> None:
    html = (
        _fomc_html(2025, "January", "28-29")
        + _fomc_html(2025, "March", "18-19")
        + _fomc_html(2025, "May", "6-7")
    )
    dates = _parse_fomc_dates(html)
    assert date(2025, 1, 29) in dates
    assert date(2025, 3, 19) in dates
    assert date(2025, 5, 7) in dates


def test_parse_fomc_dates_deduplicates() -> None:
    html = _fomc_html(2025, "January", "28-29") * 2
    dates = _parse_fomc_dates(html)
    assert dates.count(date(2025, 1, 29)) == 1


# ── upcoming_events ───────────────────────────────────────────────────────────


def test_upcoming_events_sorted() -> None:
    fomc = [date(2025, 1, 29), date(2025, 3, 19)]
    now = datetime(2025, 1, 1, 0, 0, tzinfo=UTC)
    events = upcoming_events(fomc, now)
    times = [e[0] for e in events]
    assert times == sorted(times)


def test_upcoming_events_excludes_past() -> None:
    fomc = [date(2025, 1, 29)]
    now = datetime(2025, 1, 30, 0, 0, tzinfo=UTC)
    events = upcoming_events(fomc, now)
    fomc_events = [e for e in events if e[1] == "FOMC"]
    assert not fomc_events


def test_upcoming_events_includes_both_fomc_and_nfp() -> None:
    fomc = [date(2025, 1, 29)]
    now = datetime(2025, 1, 1, 0, 0, tzinfo=UTC)
    events = upcoming_events(fomc, now, horizon_days=60)
    labels = {e[1] for e in events}
    assert "FOMC" in labels
    assert "NFP" in labels


def test_upcoming_events_fomc_at_14_et() -> None:
    fomc = [date(2025, 1, 29)]
    now = datetime(2025, 1, 1, tzinfo=UTC)
    events = upcoming_events(fomc, now)
    fomc_event = next(e for e in events if e[1] == "FOMC")
    dt_et = fomc_event[0].astimezone(_ET)
    assert dt_et.hour == 14
    assert dt_et.minute == 0


def test_upcoming_events_nfp_at_0830_et() -> None:
    fomc: list[date] = []
    now = datetime(2025, 1, 1, tzinfo=UTC)
    events = upcoming_events(fomc, now, horizon_days=40)
    nfp_event = next(e for e in events if e[1] == "NFP")
    dt_et = nfp_event[0].astimezone(_ET)
    assert dt_et.hour == 8
    assert dt_et.minute == 30


# ── CalendarListener.stream ───────────────────────────────────────────────────


def _fomc_listener(fake_now: datetime, fomc: list[date]) -> CalendarListener:
    """Helper: listener with fixed clock and mocked FOMC dates."""
    return CalendarListener(_clock=lambda: fake_now)


async def test_stream_emits_watch_then_released() -> None:
    """Stream emits watch then released for the next event."""
    fomc = [date(2025, 1, 29)]
    release_et = datetime(2025, 1, 29, 14, 0, tzinfo=_ET)
    fake_now = release_et.astimezone(UTC) - timedelta(minutes=6)
    listener = _fomc_listener(fake_now, fomc)

    collected: list[CalendarEvent] = []
    with patch("oilbot.news.calendar.get_fomc_dates", new=AsyncMock(return_value=fomc)):
        with patch("oilbot.news.calendar.asyncio.sleep", new=AsyncMock()):
            async for event in listener.stream():
                collected.append(event)
                if len(collected) == 2:
                    break

    assert len(collected) == 2
    assert collected[0].event_type == "watch"
    assert collected[0].event_name == "FOMC"
    assert collected[1].event_type == "released"
    assert collected[1].event_name == "FOMC"


async def test_stream_watch_event_text() -> None:
    fomc = [date(2025, 1, 29)]
    release_et = datetime(2025, 1, 29, 14, 0, tzinfo=_ET)
    fake_now = release_et.astimezone(UTC) - timedelta(minutes=6)
    listener = _fomc_listener(fake_now, fomc)

    with patch("oilbot.news.calendar.get_fomc_dates", new=AsyncMock(return_value=fomc)):
        with patch("oilbot.news.calendar.asyncio.sleep", new=AsyncMock()):
            async for event in listener.stream():
                assert "heads up" in event.text.lower()
                break


async def test_stream_events_are_neutral() -> None:
    fomc = [date(2025, 1, 29)]
    release_et = datetime(2025, 1, 29, 14, 0, tzinfo=_ET)
    fake_now = release_et.astimezone(UTC) - timedelta(minutes=6)
    listener = _fomc_listener(fake_now, fomc)

    with patch("oilbot.news.calendar.get_fomc_dates", new=AsyncMock(return_value=fomc)):
        with patch("oilbot.news.calendar.asyncio.sleep", new=AsyncMock()):
            async for event in listener.stream():
                assert event.default_sentiment == Sentiment.NEUTRAL
                break


async def test_stream_sleep_duration_correct() -> None:
    """10 min before release → first sleep ≈ 300s (to reach T-5min watch window)."""
    fomc = [date(2025, 1, 29)]
    release_et = datetime(2025, 1, 29, 14, 0, tzinfo=_ET)
    fake_now = release_et.astimezone(UTC) - timedelta(minutes=10)
    listener = _fomc_listener(fake_now, fomc)

    sleep_calls: list[float] = []

    async def fake_sleep(t: float) -> None:
        sleep_calls.append(t)

    with patch("oilbot.news.calendar.get_fomc_dates", new=AsyncMock(return_value=fomc)):
        with patch("oilbot.news.calendar.asyncio.sleep",
                   new=AsyncMock(side_effect=fake_sleep)):
            async for _ in listener.stream():
                break

    assert len(sleep_calls) >= 1
    assert abs(sleep_calls[0] - 300.0) < 1.0
