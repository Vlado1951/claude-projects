from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal


class Sentiment(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


@dataclass
class Classification:
    sentiment: Sentiment
    confidence: float
    rationale: str
    source: Literal["keywords", "llm", "cache", "hint", "eia"]


@dataclass
class AlertPayload:
    side: Literal["BUY", "SELL"]
    post_id: str
    post_text: str
    sentiment: Sentiment
    confidence: float
    spike_level: float
    pullback_level: float
    breakout_level: float
    breakout_at: datetime
    stop: float
    target: float
    confirmation_status: str = field(default="not_checked")
    confirmation_details: str = field(default="")
