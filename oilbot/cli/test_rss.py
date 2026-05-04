from __future__ import annotations

from oilbot.config import Config
from oilbot.news.rss import GenericRSSListener
from oilbot.persistence.db import Database


async def test_rss(config: Config, feed_url: str, name: str) -> None:
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        listener = GenericRSSListener(
            name=name,
            feed_url=feed_url,
            db=db,
            poll_interval_seconds=0,
        )
        entries = await listener._fetch()
        if not entries:
            print(f"[{name}] no entries returned by feed")
            return
        print(f"[{name}] {len(entries)} entries fetched from {feed_url}")
        for entry in entries[:5]:
            event = listener._to_event(entry)
            print(f"  id={event.id!r}")
            print(f"  posted={event.posted_at.isoformat()}")
            print(f"  text={event.text[:120]!r}")
            if event.url:
                print(f"  url={event.url}")
            print()
