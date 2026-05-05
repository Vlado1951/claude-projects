from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

import certifi
import feedparser
import httpx
import structlog

from oilbot.news.models import NewsEvent
from oilbot.persistence.db import Database
from oilbot.types import Sentiment

log = structlog.get_logger()

_MIN_BACKOFF = 5.0
_MAX_BACKOFF = 300.0


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def result(self) -> str:
        return re.sub(r"\s+", " ", "".join(self._parts)).strip()


def _strip_html(text: str) -> str:
    p = _TextExtractor()
    p.feed(text)
    return p.result()


def _parse_entry_time(entry: Any) -> datetime:
    for field in ("published", "updated"):
        raw = entry.get(field)
        if raw:
            try:
                return parsedate_to_datetime(raw).astimezone(UTC).replace(tzinfo=UTC)
            except Exception:
                pass
    return datetime.now(UTC)


def _entry_text(entry: Any) -> str:
    raw = f"{entry.get('title', '')} {entry.get('summary', '')}"
    return _strip_html(raw)


class GenericRSSListener:
    """Polls an RSS feed and emits NewsEvents for new, keyword-matching entries."""

    def __init__(
        self,
        *,
        name: str,
        feed_url: str,
        db: Database,
        poll_interval_seconds: int = 60,
        keyword_filter: list[str] | None = None,
        default_sentiment: Sentiment | None = None,
        active_window: Callable[[datetime], bool] | None = None,
    ) -> None:
        self.name = name
        self._url = feed_url
        self._db = db
        self._poll_interval = poll_interval_seconds
        self._keywords = [k.lower() for k in keyword_filter] if keyword_filter else None
        self._default_sentiment = default_sentiment
        self._active_window = active_window

    def _passes_filter(self, entry: Any) -> bool:
        if self._keywords is None:
            return True
        text = _entry_text(entry).lower()
        return any(kw in text for kw in self._keywords)

    def _to_event(self, entry: Any) -> NewsEvent:
        entry_id = entry.get("id") or entry.get("link") or entry.get("title", "")
        published = _parse_entry_time(entry)
        return NewsEvent(
            id=entry_id,
            author=self.name,
            text=_entry_text(entry).strip(),
            posted_at=published,
            received_at=datetime.now(UTC),
            url=entry.get("link", ""),
            default_sentiment=self._default_sentiment,
        )

    async def _fetch(self) -> list[Any]:
        async with httpx.AsyncClient(
            verify=certifi.where(), follow_redirects=True, timeout=15.0
        ) as client:
            response = await client.get(self._url)
            response.raise_for_status()
            content = response.text
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, feedparser.parse, content)
        if result.get("bozo") and not result.get("entries"):
            exc = result.get("bozo_exception")
            raise OSError(f"feedparser error: {exc}")
        return list(result.get("entries", []))

    async def stream(self) -> AsyncIterator[NewsEvent]:
        backoff = _MIN_BACKOFF
        while True:
            now = datetime.now(UTC)
            if self._active_window is not None and not self._active_window(now):
                log.debug("rss_source_inactive", source=self.name)
                await asyncio.sleep(self._poll_interval)
                continue

            try:
                entries = await self._fetch()
                backoff = _MIN_BACKOFF
            except Exception as exc:
                log.error("rss_fetch_error", source=self.name, error=str(exc), next_backoff=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _MAX_BACKOFF)
                continue

            for entry in entries:
                entry_id = entry.get("id") or entry.get("link") or entry.get("title", "")
                if not entry_id:
                    continue
                if self._db.is_seen(entry_id, self.name):
                    continue
                if not self._passes_filter(entry):
                    continue
                self._db.mark_seen(entry_id, self.name)
                event = self._to_event(entry)
                log.info("rss_event_received", source=self.name, entry_id=entry_id, url=event.url)
                yield event

            await asyncio.sleep(self._poll_interval)
