from __future__ import annotations

import asyncio
import signal

import structlog

from oilbot.alerts.telegram import TelegramAlerter
from oilbot.classifier.hybrid import HybridClassifier
from oilbot.config import Config
from oilbot.news.calendar import CalendarEvent, CalendarListener
from oilbot.news.configured_sources import EIA_SOURCES, RSS_SOURCES
from oilbot.news.eia import EIAReleaseListener
from oilbot.news.listener import NewsListener
from oilbot.news.models import NewsEvent
from oilbot.news.rss import GenericRSSListener
from oilbot.news.source import NewsSource
from oilbot.persistence.db import DEFAULT_DB_PATH, Database
from oilbot.price.context import PriceContext
from oilbot.price.monitor import PriceMonitor
from oilbot.strategy.engine import StrategyEngine
from oilbot.types import Sentiment

log = structlog.get_logger()

_DRAIN_TIMEOUT = 30.0


class Orchestrator:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._db = Database(DEFAULT_DB_PATH)
        self._alerter = TelegramAlerter(config)
        self._monitor = PriceMonitor(config, alerter=self._alerter)
        self._classifier = HybridClassifier(config)
        self._price_context = (
            PriceContext() if config.confirmation_mode != "off" else None
        )
        self._engine = StrategyEngine(
            config,
            self._monitor,
            self._db,
            alerter=self._alerter,
            price_context=self._price_context,
        )
        self._sources: list[NewsSource] = self._build_sources(config)
        self._stopping = False

    def _build_sources(self, config: Config) -> list[NewsSource]:
        enabled = {s.strip() for s in config.news_sources.split(",") if s.strip()}
        sources: list[NewsSource] = []
        if "truth_social" in enabled:
            sources.append(NewsListener(config, self._db, alerter=self._alerter))
        for name, cfg in RSS_SOURCES.items():
            if name in enabled:
                sources.append(
                    GenericRSSListener(
                        name=cfg.name,
                        feed_url=cfg.feed_url,
                        db=self._db,
                        poll_interval_seconds=cfg.poll_interval_seconds,
                        keyword_filter=cfg.keyword_filter,
                        default_sentiment=cfg.default_sentiment,
                        active_window=cfg.active_window,
                    )
                )
        for name, ds in EIA_SOURCES.items():
            if name in enabled:
                sources.append(
                    EIAReleaseListener(api_key=config.eia_api_key, dataset=ds)
                )
        if "calendar" in enabled:
            sources.append(CalendarListener())
        return sources

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._request_stop)

        await self._alerter.startup(
            f"oilbot starting — handle @{self._config.truth_social_handle}"
        )
        log.info(
            "orchestrator_starting",
            handle=self._config.truth_social_handle,
            ibkr_host=self._config.ibkr_host,
            ibkr_port=self._config.ibkr_port,
            llm_provider=self._config.llm_provider,
            news_sources=self._config.news_sources,
        )

        monitor_task = asyncio.create_task(self._monitor.start(), name="price_monitor")
        news_task = asyncio.create_task(self._run_news_loop(), name="news_loop")
        context_task = (
            asyncio.create_task(self._price_context.start(), name="price_context")
            if self._price_context is not None
            else None
        )

        tasks = [t for t in (monitor_task, news_task, context_task) if t is not None]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown(monitor_task, news_task, context_task)

    def _request_stop(self) -> None:
        if not self._stopping:
            self._stopping = True
            log.info("orchestrator_stopping")
            for task in asyncio.all_tasks():
                task.cancel()

    async def _run_news_loop(self) -> None:
        if not self._sources:
            log.warning("no_news_sources_enabled")
            return

        queue: asyncio.Queue[NewsEvent] = asyncio.Queue()
        producers = [
            asyncio.create_task(
                self._drain_source(source, queue), name=f"source_{source.name}"
            )
            for source in self._sources
        ]

        try:
            while True:
                event = await queue.get()
                if not self._stopping:
                    asyncio.create_task(
                        self._handle_event(event), name=f"event_{event.id}"
                    )
        except asyncio.CancelledError:
            for task in producers:
                task.cancel()
            raise

    async def _drain_source(
        self, source: NewsSource, queue: asyncio.Queue[NewsEvent]
    ) -> None:
        try:
            async for event in source.stream():
                await queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("news_source_error", source=source.name, error=str(exc))

    async def _handle_event(self, event: NewsEvent) -> None:
        if isinstance(event, CalendarEvent):
            log.info(
                "calendar_event",
                event_name=event.event_name,
                event_type=event.event_type,
            )
            await self._alerter.system(event.text)
            return

        classification = await self._classifier.classify(event)
        log.info(
            "event_classified",
            post_id=event.id,
            sentiment=classification.sentiment.value,
            source=classification.source,
        )
        if classification.sentiment == Sentiment.NEUTRAL:
            return
        await self._engine.handle(event, classification)

    async def _shutdown(
        self,
        monitor_task: asyncio.Task[None],
        news_task: asyncio.Task[None],
        context_task: asyncio.Task[None] | None,
    ) -> None:
        log.info("orchestrator_draining_strategy_tasks")
        await self._engine.drain(timeout=_DRAIN_TIMEOUT)

        for task in (monitor_task, news_task, context_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        log.info("orchestrator_shutdown_complete")
