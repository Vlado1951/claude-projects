from datetime import UTC, datetime

from oilbot.classifier.hybrid import HybridClassifier
from oilbot.config import Config
from oilbot.news.models import NewsEvent


async def classify(config: Config, text: str) -> None:
    event = NewsEvent(
        id="cli",
        author="cli",
        text=text,
        posted_at=datetime.now(UTC),
        received_at=datetime.now(UTC),
        url="",
    )
    result = await HybridClassifier().classify(event)
    print(f"Sentiment:  {result.sentiment.value}")
    print(f"Confidence: {result.confidence:.2f}")
    print(f"Source:     {result.source}")
    print(f"Rationale:  {result.rationale}")
