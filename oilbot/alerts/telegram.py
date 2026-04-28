import asyncio

import httpx
import structlog

from oilbot.alerts.formatter import format_alert, format_startup, format_system
from oilbot.config import Config
from oilbot.types import AlertPayload

log = structlog.get_logger()

_BASE = "https://api.telegram.org"
_MAX_RETRIES = 3


class TelegramAlerter:
    def __init__(self, config: Config) -> None:
        self._token = config.telegram_token
        self._chat_id = config.telegram_chat_id

    async def _send(self, text: str) -> None:
        url = f"{_BASE}/bot{self._token}/sendMessage"
        body = {"chat_id": self._chat_id, "text": text, "parse_mode": "MarkdownV2"}

        async with httpx.AsyncClient() as client:
            for attempt in range(_MAX_RETRIES):
                try:
                    resp = await client.post(url, json=body, timeout=10.0)
                except httpx.HTTPError as exc:
                    if attempt < _MAX_RETRIES - 1:
                        await asyncio.sleep(2**attempt)
                        continue
                    log.error("telegram_send_error", error=str(exc))
                    return

                if resp.status_code < 400:
                    return

                if resp.status_code >= 500:
                    if attempt < _MAX_RETRIES - 1:
                        await asyncio.sleep(2**attempt)
                        continue
                    log.error("telegram_5xx", status=resp.status_code)
                    return

                # 4xx — config problem, retrying won't help
                log.error("telegram_4xx", status=resp.status_code, body=resp.text[:200])
                return

    async def alert(self, payload: AlertPayload) -> None:
        await self._send(format_alert(payload))

    async def system(self, text: str) -> None:
        await self._send(format_system(text))

    async def startup(self, text: str) -> None:
        await self._send(format_startup(text))
