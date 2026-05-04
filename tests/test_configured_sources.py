"""Tests for configured RSS source definitions (Phase 13)."""
from __future__ import annotations

from datetime import UTC, datetime

from oilbot.news.configured_sources import RSS_SOURCES, _hurricane_season
from oilbot.types import Sentiment


def test_all_three_sources_defined() -> None:
    assert "opec_rss" in RSS_SOURCES
    assert "google_news_oil" in RSS_SOURCES
    assert "nhc_atlantic" in RSS_SOURCES


# ---------------------------------------------------------------------------
# opec_rss
# ---------------------------------------------------------------------------


def test_opec_rss_feed_url() -> None:
    assert "opec.org" in RSS_SOURCES["opec_rss"].feed_url


def test_opec_rss_no_filter_no_gate() -> None:
    cfg = RSS_SOURCES["opec_rss"]
    assert cfg.keyword_filter is None
    assert cfg.default_sentiment is None
    assert cfg.active_window is None


def test_opec_rss_poll_interval() -> None:
    assert RSS_SOURCES["opec_rss"].poll_interval_seconds == 300


# ---------------------------------------------------------------------------
# google_news_oil
# ---------------------------------------------------------------------------


def test_google_news_oil_feed_url() -> None:
    url = RSS_SOURCES["google_news_oil"].feed_url
    assert "news.google.com" in url
    assert "when:1d" in url


def test_google_news_oil_has_keyword_filter() -> None:
    kw = RSS_SOURCES["google_news_oil"].keyword_filter
    assert kw is not None
    assert "oil" in kw
    assert "opec" in kw
    assert "sanctions" in kw


def test_google_news_oil_no_default_sentiment_no_gate() -> None:
    cfg = RSS_SOURCES["google_news_oil"]
    assert cfg.default_sentiment is None
    assert cfg.active_window is None


def test_google_news_oil_poll_interval() -> None:
    assert RSS_SOURCES["google_news_oil"].poll_interval_seconds == 60


# ---------------------------------------------------------------------------
# nhc_atlantic
# ---------------------------------------------------------------------------


def test_nhc_atlantic_feed_url() -> None:
    assert "nhc.noaa.gov" in RSS_SOURCES["nhc_atlantic"].feed_url


def test_nhc_atlantic_default_sentiment_bullish() -> None:
    assert RSS_SOURCES["nhc_atlantic"].default_sentiment == Sentiment.BULLISH


def test_nhc_atlantic_has_keyword_filter() -> None:
    kw = RSS_SOURCES["nhc_atlantic"].keyword_filter
    assert kw is not None
    assert "gulf" in kw


def test_nhc_atlantic_has_active_window() -> None:
    assert RSS_SOURCES["nhc_atlantic"].active_window is not None


# ---------------------------------------------------------------------------
# Hurricane season gate (month-level)
# ---------------------------------------------------------------------------


def test_hurricane_season_active_june_through_november() -> None:
    for month in (6, 7, 8, 9, 10, 11):
        dt = datetime(2024, month, 15, tzinfo=UTC)
        assert _hurricane_season(dt), f"month {month} should be active"


def test_hurricane_season_inactive_december_through_may() -> None:
    for month in (1, 2, 3, 4, 5, 12):
        dt = datetime(2024, month, 15, tzinfo=UTC)
        assert not _hurricane_season(dt), f"month {month} should be inactive"


def test_nhc_active_window_matches_hurricane_season() -> None:
    gate = RSS_SOURCES["nhc_atlantic"].active_window
    assert gate is not None
    assert gate(datetime(2024, 8, 1, tzinfo=UTC)) is True
    assert gate(datetime(2024, 2, 1, tzinfo=UTC)) is False
