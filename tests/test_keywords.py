import pytest

from oilbot.classifier.keywords import classify_keywords
from oilbot.types import Sentiment


def test_bullish_production_cut():
    result = classify_keywords("OPEC announces production cut")
    assert result is not None
    assert result.sentiment == Sentiment.BULLISH
    assert result.source == "keywords"


def test_bearish_spr_release():
    result = classify_keywords("US releases strategic petroleum reserve")
    assert result is not None
    assert result.sentiment == Sentiment.BEARISH


def test_bearish_ceasefire():
    result = classify_keywords("Ceasefire agreement reached between warring parties")
    assert result is not None
    assert result.sentiment == Sentiment.BEARISH


def test_no_keywords_returns_none():
    result = classify_keywords("Good morning everyone")
    assert result is None


def test_both_keywords_returns_none():
    result = classify_keywords("OPEC production cut but ceasefire deal reached")
    assert result is None


def test_case_insensitive_bullish():
    result = classify_keywords("NEW SANCTIONS IMPOSED ON IRAN")
    assert result is not None
    assert result.sentiment == Sentiment.BULLISH


def test_case_insensitive_bearish():
    result = classify_keywords("SPR RELEASE ANNOUNCED BY WHITE HOUSE")
    assert result is not None
    assert result.sentiment == Sentiment.BEARISH


def test_url_stripped_no_false_positive():
    # Keyword only appears inside URL — should not match
    result = classify_keywords("Check this out https://example.com/production-cut-story")
    assert result is None


def test_keyword_in_text_not_url():
    # Keyword in text body, URL is irrelevant
    result = classify_keywords("New sanctions announced https://example.com")
    assert result is not None
    assert result.sentiment == Sentiment.BULLISH


def test_confidence_increases_with_multiple_matches():
    single = classify_keywords("sanctions imposed")
    multi = classify_keywords("sanctions embargo pipeline shutdown")
    assert single is not None
    assert multi is not None
    assert multi.confidence > single.confidence


@pytest.mark.parametrize("text", [
    "OPEC announces production cut",
    "Pipeline shutdown in Middle East",
    "Iran sanctions escalation",
])
def test_bullish_examples(text: str) -> None:
    result = classify_keywords(text)
    assert result is not None
    assert result.sentiment == Sentiment.BULLISH


@pytest.mark.parametrize("text", [
    "US releases strategic petroleum reserve",
    "Ceasefire deal reached",
    "OPEC raise output increase announced",
])
def test_bearish_examples(text: str) -> None:
    result = classify_keywords(text)
    assert result is not None
    assert result.sentiment == Sentiment.BEARISH
