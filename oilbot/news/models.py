from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from oilbot.types import Sentiment


@dataclass
class NewsEvent:
    id: str
    author: str
    text: str
    posted_at: datetime
    received_at: datetime
    url: str
    default_sentiment: Sentiment | None = field(default=None)
