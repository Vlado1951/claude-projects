from datetime import UTC, datetime

from oilbot.alerts.formatter import escape_md, format_alert
from oilbot.types import AlertPayload, Sentiment


def _payload(**overrides: object) -> AlertPayload:
    defaults: dict[str, object] = {
        "side": "BUY",
        "post_id": "abc123",
        "post_text": "OPEC announces production cut",
        "sentiment": Sentiment.BULLISH,
        "confidence": 0.82,
        "spike_level": 78.42,
        "pullback_level": 78.21,
        "breakout_level": 78.45,
        "breakout_at": datetime(2024, 1, 15, 14, 7, 32, tzinfo=UTC),
        "stop": 78.21,
        "target": 78.81,
    }
    defaults.update(overrides)
    return AlertPayload(**defaults)  # type: ignore[arg-type]


def test_buy_alert_has_green_emoji():
    result = format_alert(_payload())
    assert result.startswith("🟢")


def test_sell_alert_has_red_emoji():
    result = format_alert(_payload(side="SELL", sentiment=Sentiment.BEARISH))
    assert result.startswith("🔴")


def test_buy_alert_contains_side_label():
    result = format_alert(_payload())
    assert "BUY ALERT" in result


def test_sell_alert_contains_side_label():
    result = format_alert(_payload(side="SELL", sentiment=Sentiment.BEARISH))
    assert "SELL ALERT" in result


def test_alert_contains_prices():
    result = format_alert(_payload())
    # Prices are escaped: dots become \.
    assert "78\\.42" in result
    assert "78\\.21" in result
    assert "78\\.45" in result


def test_alert_contains_post_text():
    result = format_alert(_payload())
    assert "OPEC announces production cut" in result


def test_post_text_truncated_at_200_chars():
    long_text = "x" * 250
    result = format_alert(_payload(post_text=long_text))
    assert "x" * 201 not in result
    assert "…" in result


def test_post_text_not_truncated_when_short():
    result = format_alert(_payload(post_text="short text"))
    assert "…" not in result


def test_escape_md_escapes_special_chars():
    assert escape_md("hello.world") == r"hello\.world"
    assert escape_md("(test)") == r"\(test\)"
    assert escape_md("1+1=2") == r"1\+1\=2"
