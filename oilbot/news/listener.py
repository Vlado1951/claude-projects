import asyncio
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, Literal

import structlog
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

from oilbot.config import Config
from oilbot.news.models import NewsEvent
from oilbot.persistence.db import Database

if TYPE_CHECKING:
    from oilbot.alerts.telegram import TelegramAlerter

log = structlog.get_logger()

_API_BASE = "https://truthsocial.com/api/v1"
# chrome146 passes Cloudflare's bot detection as of 2026-05.
# If 403s return, try the next chrome* variant in curl_cffi.requests.BrowserType.
_IMPERSONATE: Literal["chrome146"] = "chrome146"
_MIN_BACKOFF = 5.0
_MAX_BACKOFF = 300.0


@dataclass
class _RawPost:
    id: str
    author: str
    text: str
    created_at: datetime
    url: str


class _HTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def get_text(self) -> str:
        return " ".join(self._parts)


def _strip_html(html: str) -> str:
    stripper = _HTMLStripper()
    stripper.feed(html)
    return re.sub(r"\s+", " ", stripper.get_text()).strip()


def _parse_feed(data: list[dict[str, Any]]) -> list[_RawPost]:
    """Parse /api/v1/accounts/{id}/statuses JSON response.

    Isolated here so it can be updated when the API format changes.
    Fields as of 2026-05: id, created_at (ISO8601 Z-suffix),
    content (HTML), url, account.username.
    """
    posts = []
    for item in data:
        created_at = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
        posts.append(
            _RawPost(
                id=item["id"],
                author=item.get("account", {}).get("username", ""),
                text=_strip_html(item.get("content", "")),
                created_at=created_at,
                url=item.get("url", ""),
            )
        )
    return posts


class NewsListener:
    name = "truth_social"

    def __init__(
        self,
        config: Config,
        db: Database,
        alerter: "TelegramAlerter | None" = None,
    ) -> None:
        self._handle = config.truth_social_handle
        self._poll_interval = config.news_poll_interval_seconds
        self._db = db
        self._alerter = alerter
        self._degraded_notified = False

    async def _resolve_account_id(self, session: "AsyncSession[Any]") -> str:
        resp = await session.get(
            f"{_API_BASE}/accounts/lookup",
            params={"acct": self._handle},
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        return str(resp.json()["id"])

    async def _fetch_statuses(
        self, session: "AsyncSession[Any]", account_id: str
    ) -> list[dict[str, Any]]:
        resp = await session.get(
            f"{_API_BASE}/accounts/{account_id}/statuses",
            params={"limit": 40, "exclude_replies": "true", "exclude_reblogs": "true"},
            headers={"Accept": "application/json"},
        )
        resp.raise_for_status()
        return list(resp.json())

    async def fetch_latest(self) -> list[NewsEvent]:
        """Fetch the most recent posts without touching the seen-posts DB. Debug use only."""
        async with AsyncSession(impersonate=_IMPERSONATE) as session:
            account_id = await self._resolve_account_id(session)
            data = await self._fetch_statuses(session, account_id)
            posts = _parse_feed(data)
            return [
                NewsEvent(
                    id=p.id,
                    author=p.author,
                    text=p.text,
                    posted_at=p.created_at,
                    received_at=datetime.now(UTC),
                    url=p.url,
                )
                for p in posts
            ]

    async def stream(self) -> AsyncIterator[NewsEvent]:
        backoff = _MIN_BACKOFF
        async with AsyncSession(impersonate=_IMPERSONATE) as session:
            account_id = await self._resolve_account_id(session)
            log.info("news_listener_startup", handle=self._handle, account_id=account_id)

            # Seed: mark all current posts seen without emitting (avoids alert storm on start)
            seed_data = await self._fetch_statuses(session, account_id)
            seed_posts = _parse_feed(seed_data)
            seeded = sum(1 for p in seed_posts if not self._db.is_seen(p.id, self.name))
            for p in seed_posts:
                if not self._db.is_seen(p.id, self.name):
                    self._db.mark_seen(p.id, self.name)
            log.info("news_seed_complete", posts_seeded=seeded)

            while True:
                await asyncio.sleep(self._poll_interval)

                # ── fetch ─────────────────────────────────────────────────────
                try:
                    data = await self._fetch_statuses(session, account_id)
                except RequestException as exc:
                    log.error("news_fetch_error", error=str(exc), next_backoff=backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _MAX_BACKOFF)
                    continue

                # ── parse ─────────────────────────────────────────────────────
                try:
                    posts = _parse_feed(data)
                except Exception as exc:
                    log.error("news_parse_error", error=str(exc), raw=str(data)[:2048])
                    if not self._degraded_notified and self._alerter:
                        await self._alerter.system(
                            "news listener degraded — manual check required"
                        )
                        self._degraded_notified = True
                    continue

                backoff = _MIN_BACKOFF  # reset on successful fetch+parse

                for post in posts:
                    if not self._db.is_seen(post.id, self.name):
                        self._db.mark_seen(post.id, self.name)
                        event = NewsEvent(
                            id=post.id,
                            author=post.author,
                            text=post.text,
                            posted_at=post.created_at,
                            received_at=datetime.now(UTC),
                            url=post.url,
                        )
                        log.info("news_received", post_id=post.id, author=post.author)
                        yield event
