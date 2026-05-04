
import structlog

from oilbot.classifier.keywords import classify_keywords
from oilbot.classifier.llm import LLMAdapter, make_adapter
from oilbot.config import Config
from oilbot.news.models import NewsEvent
from oilbot.persistence.db import DEFAULT_DB_PATH, ClassificationCache
from oilbot.types import Classification

log = structlog.get_logger()


class HybridClassifier:
    def __init__(
        self,
        config: Config,
        llm: LLMAdapter | None = None,
        cache: ClassificationCache | None = None,
    ) -> None:
        self._llm = llm or make_adapter(config.llm_provider, config.llm_api_key)
        self._cache = cache or ClassificationCache(DEFAULT_DB_PATH)

    async def classify(self, event: NewsEvent) -> Classification:
        cached = self._cache.get(event.id)
        if cached is not None:
            log.info("classification", post_id=event.id, source="cache",
                     sentiment=cached.sentiment.value)
            return cached

        result = classify_keywords(event.text)
        if result is not None:
            log.info("classification", post_id=event.id, source="keywords",
                     sentiment=result.sentiment.value)
            self._cache.save(event.id, result)
            return result

        result = await self._llm.classify_text(event.text)
        log.info("classification", post_id=event.id, source=result.source,
                 sentiment=result.sentiment.value)
        self._cache.save(event.id, result)
        return result
