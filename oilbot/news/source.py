from collections.abc import AsyncIterator
from typing import Protocol

from oilbot.news.models import NewsEvent


class NewsSource(Protocol):
    name: str

    def stream(self) -> AsyncIterator[NewsEvent]: ...
