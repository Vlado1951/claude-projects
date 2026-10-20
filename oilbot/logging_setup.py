import logging
import re
import sys
from typing import Any

import structlog

# Masks Telegram bot tokens (e.g. in the api.telegram.org/bot<id>:<token>/ URL
# that httpx logs at INFO) so secrets never reach stdout or log files.
_TOKEN_RE = re.compile(r"(bot\d+:)[A-Za-z0-9_-]{20,}")
_TOKEN_SUB = r"\1<redacted>"


def _redact(value: Any) -> Any:
    return _TOKEN_RE.sub(_TOKEN_SUB, value) if isinstance(value, str) else value


class RedactSecretsFilter(logging.Filter):
    """Strip Telegram bot tokens from log records before they are emitted.

    Foreign (stdlib) records such as httpx's "HTTP Request: %s %s" pass the URL
    as a non-str object (httpx.URL), so the token only appears once the record is
    rendered. We therefore render str-message records to their final form, redact
    that, and clear the args. Native structlog records carry a dict in ``msg`` with
    no args — those are left untouched (and redacted field-by-field is unnecessary
    because secrets never enter the event dict).
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.args and isinstance(record.msg, str):
            record.msg = record.getMessage()
            record.args = ()
        if isinstance(record.msg, str):
            record.msg = _redact(record.msg)
        return True


def setup_logging(level: str = "INFO") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    structlog.configure(
        processors=shared_processors
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(RedactSecretsFilter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(log_level)
