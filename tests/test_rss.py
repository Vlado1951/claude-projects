"""Tests for GenericRSSListener (Phase 12)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oilbot.news.rss import GenericRSSListener
from oilbot.persistence.db import Database
from oilbot.types import Sentiment


def _mock_httpx() -> MagicMock:
    """Async context-manager mock for httpx.AsyncClient that returns empty feed text."""
    response = MagicMock()
    response.text = "<rss/>"
    response.raise_for_status = MagicMock()
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


def _make_entry(
    *,
    id: str = "eid-1",
    link: str = "https://example.com/1",
    title: str = "Oil hits $80",
    summary: str = "Crude oil surges.",
    published: str = "Mon, 01 Jan 2024 12:00:00 +0000",
) -> dict[str, Any]:
    return {
        "id": id,
        "link": link,
        "title": title,
        "summary": summary,
        "published": published,
    }


def _make_feed(entries: list[dict[str, Any]], bozo: bool = False) -> MagicMock:
    feed = MagicMock()
    feed.get = lambda k, default=None: (
        entries if k == "entries" else (bozo if k == "bozo" else default)
    )
    return feed


def _listener(tmp_path: Path, **kwargs: Any) -> GenericRSSListener:
    db = Database(tmp_path / "test.db")
    defaults: dict[str, Any] = {"poll_interval_seconds": 0}
    defaults.update(kwargs)
    return GenericRSSListener(
        name="test_source",
        feed_url="https://example.com/feed",
        db=db,
        **defaults,
    )


# ---------------------------------------------------------------------------
# _to_event
# ---------------------------------------------------------------------------


def test_to_event_fields(tmp_path: Path) -> None:
    listener = _listener(tmp_path)
    entry = _make_entry()
    event = listener._to_event(entry)

    assert event.id == "eid-1"
    assert event.author == "test_source"
    assert "Oil hits $80" in event.text
    assert event.url == "https://example.com/1"
    assert event.default_sentiment is None


def test_to_event_carries_default_sentiment(tmp_path: Path) -> None:
    listener = _listener(tmp_path, default_sentiment=Sentiment.BULLISH)
    event = listener._to_event(_make_entry())
    assert event.default_sentiment == Sentiment.BULLISH


def test_to_event_falls_back_to_link_as_id(tmp_path: Path) -> None:
    listener = _listener(tmp_path)
    entry = _make_entry(id="")
    entry["id"] = ""
    event = listener._to_event(entry)
    assert event.id == "https://example.com/1"


# ---------------------------------------------------------------------------
# _passes_filter
# ---------------------------------------------------------------------------


def test_no_filter_passes_all(tmp_path: Path) -> None:
    listener = _listener(tmp_path)
    assert listener._passes_filter(_make_entry(title="Anything", summary="Here"))


def test_keyword_filter_match(tmp_path: Path) -> None:
    listener = _listener(tmp_path, keyword_filter=["crude", "opec"])
    assert listener._passes_filter(_make_entry(title="OPEC meeting today"))


def test_keyword_filter_case_insensitive(tmp_path: Path) -> None:
    listener = _listener(tmp_path, keyword_filter=["crude"])
    assert listener._passes_filter(_make_entry(title="Crude Oil Rally"))


def test_keyword_filter_no_match(tmp_path: Path) -> None:
    listener = _listener(tmp_path, keyword_filter=["opec"])
    assert not listener._passes_filter(_make_entry(title="Weather update", summary="Rain today"))


# ---------------------------------------------------------------------------
# _entry_text / HTML stripping
# ---------------------------------------------------------------------------


def test_entry_text_strips_html_tags_and_decodes_entities(tmp_path: Path) -> None:
    entry = _make_entry(
        title='Oil &amp; Gas Update',
        summary='Crude rises. <a href="https://example.com/story">Read more</a>',
    )
    listener = _listener(tmp_path)
    text = listener._to_event(entry).text
    assert "<a" not in text
    assert "href" not in text
    assert "&amp;" not in text
    assert "Oil & Gas Update" in text
    assert "Crude rises." in text
    assert "Read more" in text


# ---------------------------------------------------------------------------
# User-Agent header
# ---------------------------------------------------------------------------


async def test_fetch_sends_user_agent_header(tmp_path: Path) -> None:
    """The configured User-Agent must appear in every outgoing request."""
    listener = _listener(tmp_path, user_agent="TestBot/2.0")

    captured_kwargs: dict[str, Any] = {}

    def _capture_client(**kwargs: Any) -> MagicMock:
        captured_kwargs.update(kwargs)
        return _mock_httpx()

    with patch("oilbot.news.rss.httpx.AsyncClient", side_effect=_capture_client):
        with patch("oilbot.news.rss.feedparser.parse", return_value=_make_feed([])):
            await listener._fetch()

    assert captured_kwargs.get("headers", {}).get("User-Agent") == "TestBot/2.0"


async def test_fetch_default_user_agent_is_oilbot(tmp_path: Path) -> None:
    listener = _listener(tmp_path)
    captured_kwargs: dict[str, Any] = {}

    def _capture_client(**kwargs: Any) -> MagicMock:
        captured_kwargs.update(kwargs)
        return _mock_httpx()

    with patch("oilbot.news.rss.httpx.AsyncClient", side_effect=_capture_client):
        with patch("oilbot.news.rss.feedparser.parse", return_value=_make_feed([])):
            await listener._fetch()

    ua = captured_kwargs.get("headers", {}).get("User-Agent", "")
    assert "OilbotRSS" in ua


# ---------------------------------------------------------------------------
# _fetch
# ---------------------------------------------------------------------------


async def test_fetch_returns_entries(tmp_path: Path) -> None:
    entries = [_make_entry(id=f"e{i}") for i in range(3)]
    listener = _listener(tmp_path)

    with patch("oilbot.news.rss.httpx.AsyncClient", return_value=_mock_httpx()):
        with patch("oilbot.news.rss.feedparser.parse", return_value=_make_feed(entries)):
            result = await listener._fetch()

    assert len(result) == 3


async def test_fetch_raises_on_bozo_with_no_entries(tmp_path: Path) -> None:
    listener = _listener(tmp_path)
    feed = MagicMock()
    feed.get = lambda k, default=None: (
        True if k == "bozo" else ([] if k == "entries" else MagicMock())
    )

    with patch("oilbot.news.rss.httpx.AsyncClient", return_value=_mock_httpx()):
        with patch("oilbot.news.rss.feedparser.parse", return_value=feed):
            with pytest.raises(OSError, match="feedparser error"):
                await listener._fetch()


# ---------------------------------------------------------------------------
# stream — deduplication
# ---------------------------------------------------------------------------


async def test_stream_deduplicates(tmp_path: Path) -> None:
    """Pre-mark eid-1 as seen; verify only eid-2 is emitted, then marked seen."""
    listener = _listener(tmp_path)
    listener._db.mark_seen("eid-1", "test_source")

    entries = [_make_entry(id="eid-1"), _make_entry(id="eid-2")]
    collected: list[Any] = []

    with patch("oilbot.news.rss.httpx.AsyncClient", return_value=_mock_httpx()):
        with patch("oilbot.news.rss.feedparser.parse", return_value=_make_feed(entries)):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                async for event in listener.stream():
                    collected.append(event)
                    break

    assert len(collected) == 1
    assert collected[0].id == "eid-2"
    assert listener._db.is_seen("eid-2", "test_source")


# ---------------------------------------------------------------------------
# stream — keyword filter applied during iteration
# ---------------------------------------------------------------------------


async def test_stream_applies_keyword_filter(tmp_path: Path) -> None:
    entries = [
        _make_entry(id="e1", title="OPEC cuts production"),
        _make_entry(id="e2", title="Cat video goes viral"),
    ]
    listener = _listener(tmp_path, keyword_filter=["opec"])
    collected: list[Any] = []

    with patch("oilbot.news.rss.httpx.AsyncClient", return_value=_mock_httpx()):
        with patch("oilbot.news.rss.feedparser.parse", return_value=_make_feed(entries)):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                async for event in listener.stream():
                    collected.append(event)
                    break

    assert len(collected) == 1
    assert "OPEC" in collected[0].text


# ---------------------------------------------------------------------------
# stream — active_window gate
# ---------------------------------------------------------------------------


async def test_stream_skips_poll_when_outside_active_window(tmp_path: Path) -> None:
    always_inactive = lambda _dt: False  # noqa: E731
    listener = _listener(tmp_path, active_window=always_inactive, poll_interval_seconds=30)

    sleep_calls: list[float] = []
    call_count = 0

    async def fake_sleep(t: float) -> None:
        nonlocal call_count
        call_count += 1
        sleep_calls.append(t)
        if call_count >= 2:
            raise asyncio.CancelledError

    with patch("oilbot.news.rss.feedparser.parse") as mock_parse:
        with patch("asyncio.sleep", new=AsyncMock(side_effect=fake_sleep)):
            with pytest.raises(asyncio.CancelledError):
                async for _event in listener.stream():
                    pass

    mock_parse.assert_not_called()
    assert sleep_calls[0] == 30


# ---------------------------------------------------------------------------
# stream — backoff on fetch error
# ---------------------------------------------------------------------------


async def test_stream_backs_off_on_fetch_error(tmp_path: Path) -> None:
    listener = _listener(tmp_path)
    call_count = 0

    async def mock_fetch() -> list[Any]:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise OSError("feed down")
        raise asyncio.CancelledError

    sleep_calls: list[float] = []

    async def fake_sleep(t: float) -> None:
        sleep_calls.append(t)

    with patch.object(listener, "_fetch", new=AsyncMock(side_effect=mock_fetch)):
        with patch("asyncio.sleep", new=AsyncMock(side_effect=fake_sleep)):
            with pytest.raises(asyncio.CancelledError):
                async for _event in listener.stream():
                    pass

    # first backoff = _MIN_BACKOFF (5), second = 10
    assert sleep_calls[0] == 5.0
    assert sleep_calls[1] == 10.0
