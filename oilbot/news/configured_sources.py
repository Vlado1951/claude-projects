from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from oilbot.types import Sentiment


@dataclass
class RSSSourceConfig:
    name: str
    feed_url: str
    poll_interval_seconds: int
    keyword_filter: list[str] | None = None
    default_sentiment: Sentiment | None = None
    active_window: Callable[[datetime], bool] | None = None


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
