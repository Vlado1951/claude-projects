import asyncio

import structlog

from oilbot.config import Config
from oilbot.persistence.db import DEFAULT_DB_PATH, Database

log = structlog.get_logger()


async def run(config: Config, once_news: bool = False) -> None:
    if once_news:
        from oilbot.news.listener import NewsListener
        db = Database(DEFAULT_DB_PATH)
        listener = NewsListener(config, db)
        posts = await listener.fetch_latest()
        if not posts:
            print("No posts found.")
            return
        for post in posts[:5]:
            print(f"[{post.posted_at.strftime('%Y-%m-%d %H:%M UTC')}] {post.author}")
            print(f"  {post.text[:200]}")
            print(f"  {post.url}")
            print()
        return

    log.info(
        "starting",
        handle=config.truth_social_handle,
        ibkr_host=config.ibkr_host,
        ibkr_port=config.ibkr_port,
        llm_provider=config.llm_provider,
    )
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    log.info("shutdown")
