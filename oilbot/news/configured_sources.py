from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from oilbot.news.eia import EIADataset
from oilbot.types import Sentiment

_DEFAULT_UA = "Mozilla/5.0 (compatible; OilbotRSS/1.0)"


@dataclass
class RSSSourceConfig:
    name: str
    feed_url: str
    poll_interval_seconds: int
    keyword_filter: list[str] | None = None
    default_sentiment: Sentiment | None = None
    active_window: Callable[[datetime], bool] | None = None
    user_agent: str = _DEFAULT_UA


def _hurricane_season(dt: datetime) -> bool:
    return dt.month in (6, 7, 8, 9, 10, 11)


RSS_SOURCES: dict[str, RSSSourceConfig] = {
    "opec_rss": RSSSourceConfig(
        name="opec_rss",
        feed_url="https://www.opec.org/opec_web/en/press_room/press_release.xml",
        poll_interval_seconds=300,
    ),
    "google_news_oil": RSSSourceConfig(
        name="google_news_oil",
        feed_url=(
            "https://news.google.com/rss/search"
            "?q=oil+OR+crude+OR+OPEC+OR+sanctions+when:1d"
            "&hl=en-US&gl=US&ceid=US:en"
        ),
        poll_interval_seconds=60,
        keyword_filter=[
            "crude", "oil", "opec", "iran", "russia", "pipeline",
            "refinery", "sanctions", "embargo", "strait of hormuz", "houthi",
        ],
    ),
    "nhc_atlantic": RSSSourceConfig(
        name="nhc_atlantic",
        feed_url="https://www.nhc.noaa.gov/index-at.xml",
        poll_interval_seconds=300,
        keyword_filter=["gulf", "texas", "louisiana", "mexico"],
        default_sentiment=Sentiment.BULLISH,
        active_window=_hurricane_season,
    ),
}

# EIA v2 API datasets — https://api.eia.gov/v2/
EIA_SOURCES: dict[str, EIADataset] = {
    "eia_petroleum": EIADataset(
        name="eia_petroleum",
        metric="crude_stocks",
        unit="mbbl",
        # Weekly petroleum status report — crude oil ending stocks (SAE), national US
        api_path="petroleum/stoc/wstk/data/",
        facets={"product": ["EPC0"], "duoarea": ["NUS"], "process": ["SAE"]},
        value_multiplier=0.001,  # EIA reports in thousand barrels; convert to million
        weekday=2,               # Wednesday
        release_hour_et=10,
        release_minute_et=30,
    ),
    # Baker Hughes weekly rig count is not available at weekly frequency via EIA v2 API.
    # EIA aggregates it monthly (petroleum/crd/drill); Friday schedule retained since BH
    # publishes weekly but we detect changes via the monthly EIA update.
    "eia_rigs": EIADataset(
        name="eia_rigs",
        metric="oil_rig_count",
        unit="rigs",
        api_path="petroleum/crd/drill/data/",
        facets={"duoarea": ["NUS"], "product": ["ERTRR0"], "process": ["XR0"]},
        value_multiplier=1.0,
        frequency="monthly",
        weekday=4,               # Friday (BH publish day)
        release_hour_et=13,
        release_minute_et=0,
    ),
}
