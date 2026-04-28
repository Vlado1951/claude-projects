from oilbot.classifier.keywords import classify_keywords
from oilbot.news.models import NewsEvent
from oilbot.types import Classification, Sentiment


class HybridClassifier:
    async def classify(self, event: NewsEvent) -> Classification:
        result = classify_keywords(event.text)
        if result is not None:
            return result
        # LLM fallback added in Phase 4; default to NEUTRAL for now
        return Classification(
            sentiment=Sentiment.NEUTRAL,
            confidence=1.0,
            rationale="no keyword match; LLM fallback not configured",
            source="keywords",
        )
