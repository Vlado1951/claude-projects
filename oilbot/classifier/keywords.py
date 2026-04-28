import re
from pathlib import Path

import yaml

from oilbot.types import Classification, Sentiment

_KEYWORDS_PATH = Path(__file__).parent / "keywords.yaml"
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_WS_RE = re.compile(r"\s+")


def _load() -> tuple[list[str], list[str]]:
    with open(_KEYWORDS_PATH) as f:
        data = yaml.safe_load(f)
    return data["bullish"], data["bearish"]


_BULLISH, _BEARISH = _load()


def _normalize(text: str) -> str:
    text = _URL_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    return text.strip().lower()


def classify_keywords(text: str) -> Classification | None:
    normalized = _normalize(text)

    bullish_hits = [kw for kw in _BULLISH if kw.lower() in normalized]
    bearish_hits = [kw for kw in _BEARISH if kw.lower() in normalized]

    has_bullish = bool(bullish_hits)
    has_bearish = bool(bearish_hits)

    if has_bullish and not has_bearish:
        return Classification(
            sentiment=Sentiment.BULLISH,
            confidence=min(0.70 + 0.05 * len(bullish_hits), 0.95),
            rationale=f"matched bullish keywords: {', '.join(bullish_hits)}",
            source="keywords",
        )

    if has_bearish and not has_bullish:
        return Classification(
            sentiment=Sentiment.BEARISH,
            confidence=min(0.70 + 0.05 * len(bearish_hits), 0.95),
            rationale=f"matched bearish keywords: {', '.join(bearish_hits)}",
            source="keywords",
        )

    return None  # ambiguous or no match → fall through to LLM
