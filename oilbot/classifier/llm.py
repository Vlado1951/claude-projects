from oilbot.news.models import NewsEvent
from oilbot.types import Classification


class LLMClassifier:
    async def classify(self, event: NewsEvent) -> Classification:
        raise NotImplementedError
