import asyncio
from datetime import UTC, datetime

import structlog

from oilbot.alerts.telegram import TelegramAlerter
from oilbot.config import Config
from oilbot.news.models import NewsEvent
from oilbot.persistence.db import DEFAULT_DB_PATH, Database
from oilbot.price.monitor import PriceMonitor
from oilbot.strategy.engine import StrategyEngine
from oilbot.types import Classification, Sentiment

log = structlog.get_logger()


async def dev_trigger(config: Config, sentiment: str) -> None:
    sent = Sentiment[sentiment.upper()]
    alerter = TelegramAlerter(config)
    db = Database(DEFAULT_DB_PATH)
    monitor = PriceMonitor(config, alerter=alerter)
    engine = StrategyEngine(config, monitor, db, alerter=alerter)

    print(f"Connecting to IBKR at {config.ibkr_host}:{config.ibkr_port} …")
    monitor_task = asyncio.create_task(monitor.start())

    for _ in range(30):
        await asyncio.sleep(0.5)
        if monitor.is_connected:
            break

    if not monitor.is_connected:
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass
        print("ERROR: could not connect to IBKR within 15 s.")
        return

    now = datetime.now(UTC)
    event = NewsEvent(
        id=f"dev-trigger-{int(now.timestamp())}",
        author="dev-trigger",
        text=f"Manual {sent.value} dev trigger",
        posted_at=now,
        received_at=now,
        url="",
    )
    classification = Classification(
        sentiment=sent,
        confidence=1.0,
        rationale="manual dev trigger",
        source="keywords",
    )

    log.info("dev_trigger_start", sentiment=sent.value, post_id=event.id)
    print(
        f"\nInjecting {sent.value} event (id={event.id}).\n"
        f"Strategy window: {config.total_window_seconds} s. "
        f"Watch logs — Ctrl-C to abort.\n"
    )

    await engine.handle(event, classification)
    await engine.drain(timeout=float(config.total_window_seconds) + 15.0)

    monitor_task.cancel()
    try:
        await monitor_task
    except asyncio.CancelledError:
        pass

    print("\nDev trigger complete.")
