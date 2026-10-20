import logging

from oilbot.logging_setup import RedactSecretsFilter

_FAKE_TOKEN = "8550981550:AAEhBg3WYAuWjO5aH753FCCxie25wzfT6EE"


def _record(msg: str, args=None) -> logging.LogRecord:
    return logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=None,
    )


def test_redacts_token_in_message() -> None:
    url = f"https://api.telegram.org/bot{_FAKE_TOKEN}/sendMessage"
    rec = _record(f'HTTP Request: POST {url} "HTTP/1.1 200 OK"')
    RedactSecretsFilter().filter(rec)
    assert _FAKE_TOKEN not in rec.getMessage()
    assert "bot8550981550:<redacted>" in rec.getMessage()


def test_redacts_token_in_args() -> None:
    rec = _record(
        "HTTP Request: %s %s",
        args=("POST", f"https://api.telegram.org/bot{_FAKE_TOKEN}/sendMessage"),
    )
    RedactSecretsFilter().filter(rec)
    assert _FAKE_TOKEN not in rec.getMessage()


class _UrlLike:
    """Mimics httpx.URL — a non-str arg that only reveals the token when stringified."""

    def __init__(self, url: str) -> None:
        self._url = url

    def __str__(self) -> str:
        return self._url


def test_redacts_token_from_non_str_arg() -> None:
    # Reproduces the real leak: httpx passes the URL as an object, not a str.
    rec = _record(
        'HTTP Request: %s %s "%s %d %s"',
        args=(
            "POST",
            _UrlLike(f"https://api.telegram.org/bot{_FAKE_TOKEN}/sendMessage"),
            "HTTP/1.1",
            200,
            "OK",
        ),
    )
    RedactSecretsFilter().filter(rec)
    assert _FAKE_TOKEN not in rec.getMessage()
    assert "bot8550981550:<redacted>" in rec.getMessage()


def test_leaves_normal_messages_untouched() -> None:
    rec = _record("ibkr_connected host=127.0.0.1 port=4002")
    RedactSecretsFilter().filter(rec)
    assert rec.getMessage() == "ibkr_connected host=127.0.0.1 port=4002"


def test_filter_always_returns_true() -> None:
    assert RedactSecretsFilter().filter(_record("anything")) is True
