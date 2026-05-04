import json
from typing import Protocol, runtime_checkable

import structlog

from oilbot.types import Classification, Sentiment

log = structlog.get_logger()

_SYSTEM_PROMPT = (
    "Classify this post for crude oil price impact in the next 30 minutes. "
    "Output exactly one JSON object with keys: "
    "\"sentiment\" (BULLISH, BEARISH, or NEUTRAL), "
    "\"confidence\" (float 0.0–1.0), "
    "\"rationale\" (one sentence). "
    "Be conservative — output NEUTRAL when uncertain. "
    "Output JSON only, no other text."
)


@runtime_checkable
class LLMAdapter(Protocol):
    async def classify_text(self, text: str) -> Classification: ...


def _neutral(rationale: str = "LLM error or parse failure") -> Classification:
    return Classification(
        sentiment=Sentiment.NEUTRAL,
        confidence=0.0,
        rationale=rationale,
        source="llm",
    )


def _parse(raw: str) -> Classification:
    try:
        data = json.loads(raw.strip())
        sentiment = Sentiment(data["sentiment"].upper())
        confidence = float(data["confidence"])
        rationale = str(data["rationale"])
        return Classification(
            sentiment=sentiment,
            confidence=max(0.0, min(1.0, confidence)),
            rationale=rationale,
            source="llm",
        )
    except (json.JSONDecodeError, KeyError, ValueError, AttributeError):
        log.warning("llm_parse_failure", raw=raw[:200])
        return _neutral()


class _NullAdapter:
    async def classify_text(self, text: str) -> Classification:
        return _neutral("LLM_PROVIDER=none; no stage-2 fallback configured")


class _AnthropicAdapter:
    def __init__(self, api_key: str) -> None:
        import anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def classify_text(self, text: str) -> Classification:
        try:
            msg = await self._client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": text}],
            )
            raw: str = msg.content[0].text  # type: ignore[union-attr]
        except Exception as exc:
            log.error("llm_anthropic_error", error=str(exc))
            return _neutral()
        return _parse(raw)


class _OpenAIAdapter:
    def __init__(self, api_key: str) -> None:
        from openai import AsyncOpenAI
        self._client = AsyncOpenAI(api_key=api_key)

    async def classify_text(self, text: str) -> Classification:
        try:
            resp = await self._client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                max_tokens=256,
            )
            raw = resp.choices[0].message.content or ""
        except Exception as exc:
            log.error("llm_openai_error", error=str(exc))
            return _neutral()
        return _parse(raw)


def make_adapter(provider: str, api_key: str) -> LLMAdapter:
    if provider == "anthropic":
        return _AnthropicAdapter(api_key)
    if provider == "openai":
        return _OpenAIAdapter(api_key)
    return _NullAdapter()
