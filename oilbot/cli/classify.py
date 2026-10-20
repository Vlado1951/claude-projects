import hashlib
from datetime import UTC, datetime

from oilbot.classifier.hybrid import HybridClassifier
from oilbot.config import Config
from oilbot.news.models import NewsEvent


async def classify(config: Config, text: str) -> None:
    post_id = "cli-" + hashlib.sha256(text.encode()).hexdigest()[:16]
    event = NewsEvent(
        id=post_id,
        author="cli",
        text=text,
        posted_at=datetime.now(UTC),
        received_at=datetime.now(UTC),
        url="",
    )
    result = await HybridClassifier(config).classify(event)
    print(f"Sentiment:  {result.sentiment.value}")
    print(f"Confidence: {result.confidence:.2f}")
    print(f"Source:     {result.source}")
    print(f"Rationale:  {result.rationale}")
