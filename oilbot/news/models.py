from dataclasses import dataclass
from datetime import datetime


@dataclass
class NewsEvent:
    id: str
    author: str
    text: str
    posted_at: datetime
    received_at: datetime
    url: str
