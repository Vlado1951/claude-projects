import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog.testing

from oilbot.news.listener import NewsListener, _parse_feed, _strip_html
from oilbot.news.models import NewsEvent

# ── _strip_html ───────────────────────────────────────────────────────────────

def test_strip_html_basic():
    assert _strip_html("<p>Hello world</p>") == "Hello world"


def test_strip_html_nested():
    assert _strip_html("<p>Hello <strong>world</strong></p>") == "Hello world"


def test_strip_html_collapses_whitespace():
    assert _strip_html("<p>Hello</p><p>world</p>") == "Hello world"


def test_strip_html_plain_text_unchanged():
    assert _strip_html("No tags here") == "No tags here"


def test_strip_html_empty():
    assert _strip_html("") == ""


# ── _parse_feed ───────────────────────────────────────────────────────────────

def _make_item(
    post_id: str = "123",
    content: str = "<p>Hello</p>",
    created_at: str = "2026-05-04T10:00:00.000Z",
    url: str = "https://truthsocial.com/@test/123",
    username: str = "testuser",
) -> dict:
    return {
        "id": post_id,
        "content": content,
        "created_at": created_at,
        "url": url,
        "account": {"username": username},
    }


def test_parse_feed_returns_correct_fields():
    posts = _parse_feed([_make_item()])
    assert len(posts) == 1
    p = posts[0]
    assert p.id == "123"
    assert p.text == "Hello"
    assert p.author == "testuser"
    assert p.url == "https://truthsocial.com/@test/123"
    assert p.created_at == datetime(2026, 5, 4, 10, 0, 0, tzinfo=UTC)


def test_parse_feed_strips_html_from_content():
    posts = _parse_feed([_make_item(content="<p>TERMINATE THE FILIBUSTER</p>")])
    assert posts[0].text == "TERMINATE THE FILIBUSTER"


def test_parse_feed_multiple_items():
    items = [_make_item(post_id=str(i)) for i in range(5)]
    posts = _parse_feed(items)
    assert len(posts) == 5
    assert [p.id for p in posts] == ["0", "1", "2", "3", "4"]


def test_parse_feed_handles_missing_optional_fields():
    item = {"id": "abc", "created_at": "2026-05-04T10:00:00.000Z"}
    posts = _parse_feed([item])
    assert posts[0].id == "abc"
    assert posts[0].text == ""
    assert posts[0].author == ""
    assert posts[0].url == ""


def test_parse_feed_raises_on_missing_required_field():
    with pytest.raises((KeyError, Exception)):
        _parse_feed([{"content": "<p>no id or created_at</p>"}])


# ── NewsListener — parse error handling ───────────────────────────────────────

def _make_config(**kwargs: object) -> MagicMock:
    cfg = MagicMock()
    cfg.truth_social_handle = kwargs.get("truth_social_handle", "realDonaldTrump")
    cfg.news_poll_interval_seconds = kwargs.get("news_poll_interval_seconds", 0)
    return cfg


def _make_db(tmp_path: Path) -> MagicMock:
    from oilbot.persistence.db import Database
    return Database(tmp_path / "test.db")


async def test_parse_error_sends_telegram_once(tmp_path: Path):
    """A broken _parse_feed should trigger exactly one Telegram system message."""
    alerter = AsyncMock()
    db = _make_db(tmp_path)
    config = _make_config()
    listener = NewsListener(config, db, alerter=alerter)  # type: ignore[arg-type]

    fake_data = [_make_item()]
    broken_data = [{"id": "bad"}]  # missing created_at → parse error

    with (
        patch.object(listener, "_resolve_account_id", new=AsyncMock(return_value="999")),
        patch.object(listener, "_fetch_statuses", new=AsyncMock(side_effect=[
            fake_data,   # seed call
            broken_data, # first poll → parse error
            broken_data, # second poll → parse error again (should NOT re-notify)
        ])),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        gen = listener.stream()
        # Run until we've consumed the first poll cycle (seed + 2 failing polls)
        # We drive the generator manually; it yields nothing (broken data), so we timeout
        for _ in range(3):
            try:
                await gen.__anext__()
            except (StopAsyncIteration, Exception):
                break

        # Allow the internal loop to run a couple iterations
        import asyncio
        for _ in range(5):
            try:
                await asyncio.wait_for(gen.__anext__(), timeout=0.1)
            except (TimeoutError, StopAsyncIteration, Exception):
                pass

    alerter.system.assert_called_once()
    call_text = alerter.system.call_args[0][0]
    assert "degraded" in call_text


async def test_seen_posts_not_re_emitted(tmp_path: Path):
    """Posts already in seen_posts should not be yielded by stream()."""
    db = _make_db(tmp_path)
    config = _make_config()
    listener = NewsListener(config, db)  # type: ignore[arg-type]

    item = _make_item(post_id="already-seen")
    db.mark_seen("already-seen")

    emitted: list[NewsEvent] = []

    # Return a new-but-already-seen item, then keep returning empty lists so
    # the mock never exhausts and raises StopAsyncIteration inside the generator.
    from unittest.mock import AsyncMock as AM
    fetch_mock = AM(side_effect=[[], [item]] + [[]] * 10)

    with (
        patch.object(listener, "_resolve_account_id", new=AsyncMock(return_value="999")),
        patch.object(listener, "_fetch_statuses", new=fetch_mock),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        import asyncio
        gen = listener.stream()
        for _ in range(3):
            try:
                event = await asyncio.wait_for(gen.__anext__(), timeout=0.05)
                emitted.append(event)
            except (TimeoutError, StopAsyncIteration, RuntimeError):
                break

    assert len(emitted) == 0


async def test_parse_feed_garbage_html_raises() -> None:
    """_parse_feed raises when given HTML-string items instead of dicts."""
    import pytest
    with pytest.raises((TypeError, KeyError)):
        _parse_feed(["<html>bad</html>"])  # type: ignore[list-item]


async def test_listener_logs_parse_error_not_crash(tmp_path: Path) -> None:
    """stream() logs news_parse_error and keeps running — does not propagate the exception."""
    db = _make_db(tmp_path)
    config = _make_config()
    listener = NewsListener(config, db)  # type: ignore[arg-type]

    # Feed: HTML strings in the list → _parse_feed raises TypeError
    garbage: list[Any] = ["<html>bad</html>"]
    fetch_mock = AsyncMock(side_effect=[[], garbage] + [[]] * 10)

    with structlog.testing.capture_logs() as cap_logs:
        with (
            patch.object(listener, "_resolve_account_id", new=AsyncMock(return_value="999")),
            patch.object(listener, "_fetch_statuses", new=fetch_mock),
            patch("asyncio.sleep", new=AsyncMock()),
        ):
            gen = listener.stream()
            for _ in range(6):
                try:
                    await asyncio.wait_for(gen.__anext__(), timeout=0.05)
                except (TimeoutError, StopAsyncIteration, RuntimeError):
                    pass

    error_events = [e for e in cap_logs if e.get("log_level") == "error"]
    assert any(e.get("event") == "news_parse_error" for e in error_events), (
        f"Expected news_parse_error in logs; got: {cap_logs}"
    )


async def test_listener_degraded_telegram_exactly_once(tmp_path: Path) -> None:
    """Telegram 'degraded' alert fires once even across repeated parse failures."""
    alerter = AsyncMock()
    db = _make_db(tmp_path)
    config = _make_config()
    listener = NewsListener(config, db, alerter=alerter)  # type: ignore[arg-type]

    # Three polls in a row all return unparseable HTML garbage
    garbage: list[Any] = ["<html>bad</html>"]
    fetch_mock = AsyncMock(
        side_effect=[[], garbage, garbage, garbage] + [[]] * 10
    )

    with (
        patch.object(listener, "_resolve_account_id", new=AsyncMock(return_value="999")),
        patch.object(listener, "_fetch_statuses", new=fetch_mock),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        gen = listener.stream()
        for _ in range(10):
            try:
                await asyncio.wait_for(gen.__anext__(), timeout=0.05)
            except (TimeoutError, StopAsyncIteration, RuntimeError):
                pass

    alerter.system.assert_called_once()
    assert "degraded" in alerter.system.call_args[0][0]


async def test_new_post_is_emitted(tmp_path: Path):
    """A new post not in seen_posts should be yielded."""
    db = _make_db(tmp_path)
    config = _make_config()
    listener = NewsListener(config, db)  # type: ignore[arg-type]

    item = _make_item(post_id="new-post-1", content="<p>OPEC cut</p>")

    with (
        patch.object(listener, "_resolve_account_id", new=AsyncMock(return_value="999")),
        patch.object(listener, "_fetch_statuses", new=AsyncMock(side_effect=[
            [],      # empty seed
            [item],  # first poll — new post
        ])),
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        import asyncio
        gen = listener.stream()
        event = await asyncio.wait_for(gen.__anext__(), timeout=1.0)

    assert event.id == "new-post-1"
    assert event.text == "OPEC cut"
    assert db.is_seen("new-post-1")
