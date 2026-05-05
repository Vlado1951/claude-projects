from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import certifi
import httpx
import structlog

from oilbot.news.models import NewsEvent

log = structlog.get_logger()

_ET = ZoneInfo("America/New_York")
_PRE_WINDOW_SECS = 5 * 60   # start polling 5 min before scheduled release
_POLL_INTERVAL_SECS = 30.0
_MAX_WINDOW_SECS = 30 * 60  # give up 30 min after scheduled release


@dataclass
class EIAReleaseEvent(NewsEvent):
    metric: str = field(default="")
    delta: float = field(default=0.0)
    unit: str = field(default="")


@dataclass
class EIADataset:
    name: str
    metric: str
    unit: str
    api_path: str          # path under https://api.eia.gov/v2/
    facets: dict[str, list[str]]
    value_multiplier: float  # e.g. 0.001 to convert kbbl → mbbl
    weekday: int             # 0=Mon … 4=Fri
    release_hour_et: int
    release_minute_et: int
    frequency: str = "weekly"  # EIA API frequency param


def _next_release_time(dataset: EIADataset, now: datetime) -> datetime:
    """Next scheduled release datetime in UTC (may be in the current window)."""
    now_et = now.astimezone(_ET)
    days_ahead = (dataset.weekday - now_et.weekday()) % 7
    candidate = now_et.replace(
        hour=dataset.release_hour_et,
        minute=dataset.release_minute_et,
        second=0,
        microsecond=0,
    ) + timedelta(days=days_ahead)
    # Past the 30-min post-release window → advance to next week
    if now_et >= candidate + timedelta(seconds=_MAX_WINDOW_SECS):
        candidate += timedelta(days=7)
    return candidate.astimezone(UTC)


class EIAReleaseListener:
    """Polls the EIA v2 API on a weekly schedule and emits EIAReleaseEvents."""

    def __init__(
        self,
        *,
        api_key: str,
        dataset: EIADataset,
        base_url: str = "https://api.eia.gov/v2/",
    ) -> None:
        self.name = dataset.name
        self._api_key = api_key
        self._dataset = dataset
        self._base_url = base_url
        self._last_period: str | None = None

    async def fetch_latest(self) -> tuple[str, float] | None:
        """Return (period, delta) or None when data is unavailable."""
        ds = self._dataset
        params: dict[str, str | int] = {
            "api_key": self._api_key,
            "frequency": self._dataset.frequency,
            "data[0]": "value",
            "sort[0][column]": "period",
            "sort[0][direction]": "desc",
            "length": 2,
        }
        for facet_key, values in ds.facets.items():
            for i, v in enumerate(values):
                params[f"facets[{facet_key}][{i}]"] = v

        url = self._base_url + ds.api_path
        async with httpx.AsyncClient(
            verify=certifi.where(), timeout=15.0, follow_redirects=True
        ) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            body = resp.json()

        rows = body.get("response", {}).get("data", [])
        if len(rows) < 2:
            log.warning("eia_insufficient_data", source=self.name, rows=len(rows))
            return None

        v0 = rows[0].get("value")
        v1 = rows[1].get("value")
        if v0 is None or v1 is None:
            log.warning("eia_null_value", source=self.name)
            return None

        period: str = rows[0].get("period", "")
        delta = (float(v0) - float(v1)) * ds.value_multiplier
        return period, delta

    def make_event(self, period: str, delta: float) -> EIAReleaseEvent:
        ds = self._dataset
        direction = "draw" if delta < 0 else "build"
        text = (
            f"EIA {ds.metric}: {direction} of {abs(delta):.2f} {ds.unit} "
            f"(week of {period})"
        )
        now = datetime.now(UTC)
        return EIAReleaseEvent(
            id=f"{ds.name}_{period}",
            author=ds.name,
            text=text,
            posted_at=now,
            received_at=now,
            url=f"{self._base_url}{ds.api_path}",
            metric=ds.metric,
            delta=delta,
            unit=ds.unit,
        )

    async def stream(self) -> AsyncIterator[EIAReleaseEvent]:
        while True:
            now = datetime.now(UTC)
            release_time = _next_release_time(self._dataset, now)
            window_start = release_time - timedelta(seconds=_PRE_WINDOW_SECS)

            if now < window_start:
                sleep_secs = (window_start - now).total_seconds()
                log.info(
                    "eia_waiting_for_release",
                    source=self.name,
                    next_release=release_time.isoformat(),
                    sleep_seconds=int(sleep_secs),
                )
                await asyncio.sleep(sleep_secs)

            deadline = release_time + timedelta(seconds=_MAX_WINDOW_SECS)
            while datetime.now(UTC) < deadline:
                try:
                    result = await self.fetch_latest()
                except Exception as exc:
                    log.error("eia_fetch_error", source=self.name, error=str(exc))
                    await asyncio.sleep(_POLL_INTERVAL_SECS)
                    continue

                if result is not None:
                    period, delta = result
                    if period != self._last_period:
                        self._last_period = period
                        event = self.make_event(period, delta)
                        log.info(
                            "eia_release_received",
                            source=self.name,
                            period=period,
                            delta=round(delta, 3),
                            unit=self._dataset.unit,
                        )
                        yield event
                        break

                await asyncio.sleep(_POLL_INTERVAL_SECS)
            else:
                log.warning("eia_release_window_expired", source=self.name)
