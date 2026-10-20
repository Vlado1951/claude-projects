from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from oilbot.config import Config
from oilbot.news.calendar import get_fomc_dates, nfp_dates, upcoming_events

_ET = ZoneInfo("America/New_York")


async def test_calendar(config: Config) -> None:
    print("Fetching FOMC calendar…")
    fomc = await get_fomc_dates()

    now = datetime.now(UTC)
    events = upcoming_events(fomc, now, horizon_days=365)

    fomc_shown = 0
    nfp_shown = 0
    print("\nNext scheduled events:")
    for dt_utc, label in events:
        if label == "FOMC" and fomc_shown < 5:
            fomc_shown += 1
            _print_event(dt_utc, label)
        elif label == "NFP" and nfp_shown < 5:
            nfp_shown += 1
            _print_event(dt_utc, label)
        if fomc_shown >= 5 and nfp_shown >= 5:
            break

    today = now.astimezone(_ET).date()
    nfp = nfp_dates(today, count=5)
    if nfp_shown < 5:
        print("\nNext NFP dates (computed):")
        for d in nfp[:5]:
            print(f"  NFP  {d.isoformat()} 08:30 ET")


def _print_event(dt_utc: datetime, label: str) -> None:
    dt_et = dt_utc.astimezone(_ET)
    print(f"  {label:<5} {dt_et.strftime('%Y-%m-%d %H:%M ET')}")
