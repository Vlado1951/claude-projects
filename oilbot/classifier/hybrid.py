
import structlog

from oilbot.classifier.keywords import classify_keywords
from oilbot.classifier.llm import LLMAdapter, make_adapter
from oilbot.config import Config
from oilbot.news.eia import EIAReleaseEvent
from oilbot.news.models import NewsEvent
from oilbot.persistence.db import DEFAULT_DB_PATH, ClassificationCache
from oilbot.types import Classification, Sentiment

log = structlog.get_logger()


class HybridClassifier:
    def __init__(
        self,
        config: Config,
        llm: LLMAdapter | None = None,
        cache: ClassificationCache | None = None,
    ) -> None:
        self._config = config
        self._llm = llm or make_adapter(config.llm_provider, config.llm_api_key)
        self._cache = cache or ClassificationCache(DEFAULT_DB_PATH)

    def _classify_eia(self, event: EIAReleaseEvent) -> Classification:
        if event.unit == "rigs":
            threshold = float(self._config.eia_rig_threshold)
        else:
            threshold = self._config.eia_crude_threshold_mbbl

        abs_delta = abs(event.delta)
        if event.delta < 0 and abs_delta >= threshold:
            sentiment = Sentiment.BULLISH
            rationale = f"{event.metric} draw of {abs_delta:.2f} {event.unit}"
        elif event.delta > 0 and abs_delta >= threshold:
            sentiment = Sentiment.BEARISH
            rationale = f"{event.metric} build of {abs_delta:.2f} {event.unit}"
        else:
            sentiment = Sentiment.NEUTRAL
            rationale = (
                f"{event.metric} change of {event.delta:.2f} {event.unit} — below threshold"
            )

        confidence = min(0.95, 0.5 + abs_delta / (2 * threshold)) if threshold > 0 else 0.5
        cls = Classification(
            sentiment=sentiment,
            confidence=round(confidence, 3),
            rationale=rationale,
            source="eia",
        )
        log.info("classification", post_id=event.id, source="eia", sentiment=sentiment.value)
        return cls

    async def classify(self, event: NewsEvent) -> Classification:
        if isinstance(event, EIAReleaseEvent):
            return self._classify_eia(event)

        if event.default_sentiment is not None:
            hint = Classification(
                sentiment=event.default_sentiment,
                confidence=1.0,
                rationale="default_sentiment hint",
                source="hint",
            )
            log.info("classification", post_id=event.id, source="hint",
                     sentiment=hint.sentiment.value)
            return hint

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
