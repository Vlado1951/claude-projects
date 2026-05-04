from __future__ import annotations

import tempfile
from pathlib import Path

from oilbot.config import Config
from oilbot.news.rss import GenericRSSListener
from oilbot.persistence.db import Database


async def test_rss(
    config: Config,
    source: str | None,
    feed_url: str | None,
    name: str | None,
) -> None:
    if source is not None:
        from oilbot.news.configured_sources import RSS_SOURCES

        if source not in RSS_SOURCES:
            known = ", ".join(sorted(RSS_SOURCES))
            print(f"Unknown source: {source!r}. Available: {known}")
            return
        cfg = RSS_SOURCES[source]
        resolved_url = cfg.feed_url
        resolved_name = cfg.name
    elif feed_url is not None and name is not None:
        resolved_url = feed_url
        resolved_name = name
    else:
        print("Usage: test-rss <source-name>  OR  test-rss --feed-url <url> --name <name>")
        return

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        listener = GenericRSSListener(
            name=resolved_name,
            feed_url=resolved_url,
            db=db,
            poll_interval_seconds=0,
        )
        entries = await listener._fetch()
        if not entries:
            print(f"[{resolved_name}] no entries returned by feed")
            return
        print(f"[{resolved_name}] {len(entries)} entries fetched from {resolved_url}")
        for entry in entries[:5]:
            event = listener._to_event(entry)
            print(f"  id={event.id!r}")
            print(f"  posted={event.posted_at.isoformat()}")
            print(f"  text={event.text[:120]!r}")
            if event.url:
                print(f"  url={event.url}")
            print()
