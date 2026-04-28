from collections.abc import AsyncIterator

from oilbot.news.models import NewsEvent


class NewsListener:
    async def stream(self) -> AsyncIterator[NewsEvent]:
        raise NotImplementedError
        yield  # make this an async generator
