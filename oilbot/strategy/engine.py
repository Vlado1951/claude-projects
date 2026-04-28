from oilbot.news.models import NewsEvent
from oilbot.types import Classification


class StrategyEngine:
    async def handle(self, event: NewsEvent, classification: Classification) -> None:
        raise NotImplementedError
