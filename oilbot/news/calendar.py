from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import certifi
import httpx
import structlog

from oilbot.news.models import NewsEvent
from oilbot.types import Sentiment

log = structlog.get_logger()

_ET = ZoneInfo("America/New_York")
_FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
_CACHE_PATH = Path("data/calendar.json")
_CACHE_TTL_DAYS = 7
_FOMC_RELEASE_HOUR_ET = 14   # 2:00 PM ET — FOMC statement
_NFP_RELEASE_HOUR_ET = 8
_NFP_RELEASE_MINUTE_ET = 30  # 8:30 AM ET
_PRE_EVENT_SECS = 5 * 60     # emit "watch" 5 min before
_POST_EVENT_SECS = 30        # emit "released" 30 s after

_MONTH_NUMS = {m: i + 1 for i, m in enumerate([
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
])}


@dataclass
class CalendarEvent(NewsEvent):
    event_type: Literal["watch", "released"] = field(default="watch")
    event_name: str = field(default="")


# ── FOMC scraping ─────────────────────────────────────────────────────────────


class _FOMCHTMLParser(HTMLParser):
    """Parses the Fed FOMC calendar page using CSS class names."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.dates: list[date] = []
        self._year: int | None = None
        self._month: str | None = None
        self._in_year_heading = False
        self._in_month_div = False
        self._in_date_div = False
        self._in_strong = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        cls = dict(attrs).get("class") or ""
        if tag == "h4":
            self._in_year_heading = True
        if "fomc-meeting__month" in cls:
            self._in_month_div = True
        if "fomc-meeting__date" in cls:
            self._in_date_div = True
        if tag == "strong":
            self._in_strong = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "h4":
            self._in_year_heading = False
        if tag == "strong":
            self._in_strong = False

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if not text:
            return
        if self._in_year_heading:
            m = re.search(r"(20\d{2})", text)
            if m:
                self._year = int(m.group(1))
        if self._in_month_div and self._in_strong and text in _MONTH_NUMS:
            self._month = text
            self._in_month_div = False
        if self._in_date_div and self._year and self._month:
            # Date cell may contain "27-28", "16-17*", "22 (notation vote)", "30-1"
            # Extract the last run of digits as the meeting end-day.
            digits = re.findall(r"\d+", text)
            if digits:
                last_day = int(digits[-1])
                try:
                    self.dates.append(date(self._year, _MONTH_NUMS[self._month], last_day))
                except ValueError:
                    pass
            self._in_date_div = False


def _parse_fomc_dates(html: str) -> list[date]:
    """Extract FOMC meeting end-dates from the Fed calendar HTML."""
    parser = _FOMCHTMLParser()
    parser.feed(html)
    return sorted(set(parser.dates))


async def _fetch_fomc_dates() -> list[date]:
    async with httpx.AsyncClient(
        verify=certifi.where(), follow_redirects=True, timeout=15.0
    ) as client:
        resp = await client.get(_FOMC_URL)
        resp.raise_for_status()
        html = resp.text
    dates = _parse_fomc_dates(html)
    log.info("fomc_dates_fetched", count=len(dates))
    return dates


def _load_cache() -> tuple[list[date], datetime | None]:
    if not _CACHE_PATH.exists():
        return [], None
    try:
        raw = json.loads(_CACHE_PATH.read_text())
        dates = [date.fromisoformat(d) for d in raw.get("fomc", [])]
        fetched_at = datetime.fromisoformat(raw["fetched_at"]) if "fetched_at" in raw else None
        return dates, fetched_at
    except Exception:
        return [], None


def _save_cache(dates: list[date]) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps({
        "fomc": [d.isoformat() for d in dates],
        "fetched_at": datetime.now(UTC).isoformat(),
    }))


async def get_fomc_dates(force_refresh: bool = False) -> list[date]:
    """Return FOMC meeting end-dates, using cache when fresh."""
    cached_dates, fetched_at = _load_cache()
    cache_stale = (
        fetched_at is None
        or (datetime.now(UTC) - fetched_at) > timedelta(days=_CACHE_TTL_DAYS)
    )
    if not force_refresh and not cache_stale and cached_dates:
        return cached_dates
    try:
        dates = await _fetch_fomc_dates()
        _save_cache(dates)
        return dates
    except Exception as exc:
        log.error("fomc_fetch_failed", error=str(exc))
        return cached_dates  # fall back to potentially stale cache


# ── NFP date computation ──────────────────────────────────────────────────────


def _first_friday(year: int, month: int) -> date:
    """Return the first Friday of the given year/month."""
    d = date(year, month, 1)
    days_until_friday = (4 - d.weekday()) % 7  # Friday = weekday 4
    return d + timedelta(days=days_until_friday)


def nfp_dates(from_date: date, count: int = 24) -> list[date]:
    """Return the next `count` NFP release dates from `from_date`."""
    results: list[date] = []
    year, month = from_date.year, from_date.month
    while len(results) < count:
        d = _first_friday(year, month)
        if d >= from_date:
            results.append(d)
        month += 1
        if month > 12:
            month = 1
            year += 1
    return results


# ── Upcoming event list ───────────────────────────────────────────────────────


def _to_et_datetime(d: date, hour: int, minute: int) -> datetime:
    return datetime(d.year, d.month, d.day, hour, minute, tzinfo=_ET)


def upcoming_events(
    fomc: list[date],
    now: datetime,
    horizon_days: int = 90,
) -> list[tuple[datetime, str]]:
    """Return (release_datetime_utc, label) pairs sorted by time, within horizon."""
    cutoff = now + timedelta(days=horizon_days)
    events: list[tuple[datetime, str]] = []

    today = now.astimezone(_ET).date()
    for d in fomc:
        dt_et = _to_et_datetime(d, _FOMC_RELEASE_HOUR_ET, 0)
        dt_utc = dt_et.astimezone(UTC)
        if now <= dt_utc < cutoff:
            events.append((dt_utc, "FOMC"))

    for d in nfp_dates(today, count=6):
        dt_et = _to_et_datetime(d, _NFP_RELEASE_HOUR_ET, _NFP_RELEASE_MINUTE_ET)
        dt_utc = dt_et.astimezone(UTC)
        if now <= dt_utc < cutoff:
            events.append((dt_utc, "NFP"))

    return sorted(events)


# ── CalendarListener ──────────────────────────────────────────────────────────


class CalendarListener:
    """Emits watch/released CalendarEvents for FOMC and NFP releases."""

    def __init__(
        self,
        *,
        _clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.name = "calendar"
        self._clock = _clock or (lambda: datetime.now(UTC))

    def _make_event(
        self,
        event_name: str,
        event_type: Literal["watch", "released"],
        release_time: datetime,
    ) -> CalendarEvent:
        if event_type == "watch":
            text = f"{event_name} release in 5 minutes — heads up"
        else:
            text = f"{event_name} data just released"
        now = self._clock()
        eid = f"calendar_{event_name.lower()}_{release_time.date().isoformat()}_{event_type}"
        return CalendarEvent(
            id=eid,
            author="calendar",
            text=text,
            posted_at=now,
            received_at=now,
            url="",
            default_sentiment=Sentiment.NEUTRAL,
            event_type=event_type,
            event_name=event_name,
        )

    async def stream(self) -> AsyncIterator[CalendarEvent]:
        fomc = await get_fomc_dates()
        last_refresh = self._clock()

        while True:
            now = self._clock()

            if (now - last_refresh) > timedelta(days=_CACHE_TTL_DAYS):
                fomc = await get_fomc_dates(force_refresh=True)
                last_refresh = now

            events = upcoming_events(fomc, now)
            if not events:
                # Nothing in the 90-day window; sleep a day and retry
                log.info("calendar_no_upcoming_events", sleep_hours=24)
                await asyncio.sleep(86400)
                continue

            release_time, label = events[0]
            watch_time = release_time - timedelta(seconds=_PRE_EVENT_SECS)
            sleep_to_watch = max(0.0, (watch_time - now).total_seconds())

            log.info(
                "calendar_next_event",
                label=label,
                release_time=release_time.isoformat(),
                sleep_seconds=int(sleep_to_watch),
            )
            await asyncio.sleep(sleep_to_watch)

            yield self._make_event(label, "watch", release_time)

            await asyncio.sleep(_PRE_EVENT_SECS + _POST_EVENT_SECS)

            yield self._make_event(label, "released", release_time)
