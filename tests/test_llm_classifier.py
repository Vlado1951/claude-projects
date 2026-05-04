from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

from oilbot.classifier.hybrid import HybridClassifier
from oilbot.classifier.llm import _NullAdapter, _parse, make_adapter
from oilbot.config import Config
from oilbot.news.models import NewsEvent
from oilbot.persistence.db import ClassificationCache
from oilbot.types import Sentiment

# ── _parse ────────────────────────────────────────────────────────────────────

def test_parse_bullish():
    raw = '{"sentiment": "BULLISH", "confidence": 0.8, "rationale": "Oil supply cut."}'
    result = _parse(raw)
    assert result.sentiment == Sentiment.BULLISH
    assert result.confidence == 0.8
    assert result.rationale == "Oil supply cut."
    assert result.source == "llm"


def test_parse_bearish():
    raw = '{"sentiment": "BEARISH", "confidence": 0.7, "rationale": "SPR release."}'
    result = _parse(raw)
    assert result.sentiment == Sentiment.BEARISH


def test_parse_neutral():
    raw = '{"sentiment": "NEUTRAL", "confidence": 0.5, "rationale": "Unclear impact."}'
    result = _parse(raw)
    assert result.sentiment == Sentiment.NEUTRAL


def test_parse_case_insensitive_sentiment():
    raw = '{"sentiment": "bullish", "confidence": 0.9, "rationale": "test"}'
    result = _parse(raw)
    assert result.sentiment == Sentiment.BULLISH


def test_parse_confidence_clamped():
    raw = '{"sentiment": "BULLISH", "confidence": 1.5, "rationale": "test"}'
    result = _parse(raw)
    assert result.confidence == 1.0


def test_parse_invalid_json_returns_neutral():
    result = _parse("not json at all")
    assert result.sentiment == Sentiment.NEUTRAL
    assert result.confidence == 0.0


def test_parse_missing_key_returns_neutral():
    result = _parse('{"sentiment": "BULLISH"}')
    assert result.sentiment == Sentiment.NEUTRAL


def test_parse_invalid_sentiment_returns_neutral():
    raw = '{"sentiment": "VERY_BULLISH", "confidence": 0.9, "rationale": "test"}'
    result = _parse(raw)
    assert result.sentiment == Sentiment.NEUTRAL


def test_parse_extra_text_around_json():
    raw = 'Here is my answer:\n{"sentiment": "BEARISH", "confidence": 0.6, "rationale": "r"}\n'
    # strip() only; extra leading text will cause parse failure → NEUTRAL
    result = _parse(raw)
    assert result.sentiment == Sentiment.NEUTRAL


# ── _NullAdapter ──────────────────────────────────────────────────────────────

async def test_null_adapter_returns_neutral():
    adapter = _NullAdapter()
    result = await adapter.classify_text("anything")
    assert result.sentiment == Sentiment.NEUTRAL


def test_make_adapter_none_returns_null():
    adapter = make_adapter("none", "")
    assert isinstance(adapter, _NullAdapter)


# ── ClassificationCache ───────────────────────────────────────────────────────

def test_cache_miss_returns_none(tmp_path: Path):
    cache = ClassificationCache(tmp_path / "test.db")
    assert cache.get("unknown-id") is None


def test_cache_save_and_retrieve(tmp_path: Path):
    from oilbot.types import Classification
    cache = ClassificationCache(tmp_path / "test.db")
    c = Classification(
        sentiment=Sentiment.BULLISH,
        confidence=0.85,
        rationale="test rationale",
        source="llm",
    )
    cache.save("post-1", c)
    result = cache.get("post-1")
    assert result is not None
    assert result.sentiment == Sentiment.BULLISH
    assert result.confidence == 0.85
    assert result.rationale == "test rationale"
    assert result.source == "cache"


def test_cache_overwrites_on_duplicate(tmp_path: Path):
    from oilbot.types import Classification
    cache = ClassificationCache(tmp_path / "test.db")
    c1 = Classification(Sentiment.BULLISH, 0.8, "first", "llm")
    c2 = Classification(Sentiment.BEARISH, 0.9, "second", "llm")
    cache.save("post-1", c1)
    cache.save("post-1", c2)
    result = cache.get("post-1")
    assert result is not None
    assert result.sentiment == Sentiment.BEARISH


# ── HybridClassifier ──────────────────────────────────────────────────────────

def _make_config(**kwargs: object) -> Config:
    defaults = {
        "telegram_token": "x",
        "telegram_chat_id": "1",
        "llm_provider": "none",
        "llm_api_key": "",
    }
    defaults.update(kwargs)
    return Config(**defaults)  # type: ignore[arg-type]


def _event(text: str, post_id: str = "test-post") -> NewsEvent:
    return NewsEvent(
        id=post_id,
        author="cli",
        text=text,
        posted_at=datetime.now(UTC),
        received_at=datetime.now(UTC),
        url="",
    )


async def test_hybrid_keyword_match_no_llm_call(tmp_path: Path):
    mock_llm = AsyncMock()
    cache = ClassificationCache(tmp_path / "test.db")
    config = _make_config()
    classifier = HybridClassifier(config, llm=mock_llm, cache=cache)

    result = await classifier.classify(_event("OPEC announces production cut"))
    assert result.sentiment == Sentiment.BULLISH
    mock_llm.classify_text.assert_not_called()


async def test_hybrid_llm_called_when_no_keyword_match(tmp_path: Path):
    from oilbot.types import Classification
    mock_llm = AsyncMock()
    mock_llm.classify_text.return_value = Classification(
        sentiment=Sentiment.BEARISH,
        confidence=0.7,
        rationale="LLM says bearish",
        source="llm",
    )
    cache = ClassificationCache(tmp_path / "test.db")
    config = _make_config()
    classifier = HybridClassifier(config, llm=mock_llm, cache=cache)

    result = await classifier.classify(_event("Good morning everyone"))
    assert result.sentiment == Sentiment.BEARISH
    mock_llm.classify_text.assert_called_once()


async def test_hybrid_cache_hit_skips_keyword_and_llm(tmp_path: Path):
    from oilbot.types import Classification
    mock_llm = AsyncMock()
    cache = ClassificationCache(tmp_path / "test.db")
    cached = Classification(Sentiment.BULLISH, 0.9, "cached", "llm")
    cache.save("test-post", cached)

    config = _make_config()
    classifier = HybridClassifier(config, llm=mock_llm, cache=cache)

    result = await classifier.classify(_event("Good morning everyone", post_id="test-post"))
    assert result.sentiment == Sentiment.BULLISH
    assert result.source == "cache"
    mock_llm.classify_text.assert_not_called()


async def test_hybrid_none_provider_returns_neutral_for_ambiguous(tmp_path: Path):
    cache = ClassificationCache(tmp_path / "test.db")
    config = _make_config(llm_provider="none")
    classifier = HybridClassifier(config, cache=cache)

    result = await classifier.classify(_event("Good morning everyone"))
    assert result.sentiment == Sentiment.NEUTRAL


async def test_hybrid_result_is_cached_after_llm_call(tmp_path: Path):
    from oilbot.types import Classification
    mock_llm = AsyncMock()
    mock_llm.classify_text.return_value = Classification(
        sentiment=Sentiment.BULLISH, confidence=0.8, rationale="llm result", source="llm"
    )
    cache = ClassificationCache(tmp_path / "test.db")
    config = _make_config()
    classifier = HybridClassifier(config, llm=mock_llm, cache=cache)

    await classifier.classify(_event("ambiguous text", post_id="p1"))
    # Second call — should hit cache, not LLM
    await classifier.classify(_event("ambiguous text", post_id="p1"))

    assert mock_llm.classify_text.call_count == 1
    assert cache.get("p1") is not None
