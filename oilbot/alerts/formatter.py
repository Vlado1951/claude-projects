import re
from datetime import UTC

from oilbot.types import AlertPayload

_MD_SPECIAL = re.compile(r"([_*\[\]()~`>#+=|{}.!\-])")


def escape_md(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", text)


def _bold(text: str) -> str:
    return f"*{escape_md(text)}*"


def _fmt_price(price: float) -> str:
    return escape_md(f"{price:.2f}")


def format_alert(payload: AlertPayload) -> str:
    emoji = "🟢" if payload.side == "BUY" else "🔴"
    side_label = "BUY ALERT" if payload.side == "BUY" else "SELL ALERT"
    spike_label = "Spike high" if payload.side == "BUY" else "Dump low"
    pullback_label = "Pullback low" if payload.side == "BUY" else "Bounce high"

    post_text = payload.post_text[:200]
    if len(payload.post_text) > 200:
        post_text += "…"

    breakout_time = payload.breakout_at.astimezone(UTC).strftime("%H:%M:%S UTC")

    lines = [
        f"{emoji} {_bold(f'{side_label} — MCL')}",
        "Trigger: "
        + escape_md(
            f"Trump post ({payload.sentiment.value}, {payload.confidence:.2f})"
        ),
        f'"{escape_md(post_text)}"',
        f"{escape_md(spike_label)}: {_fmt_price(payload.spike_level)}",
        f"{escape_md(pullback_label)}: {_fmt_price(payload.pullback_level)}",
        f"Breakout: {_fmt_price(payload.breakout_level)} @ {escape_md(breakout_time)}",
        f"Suggested stop: {_fmt_price(payload.stop)}",
        "Suggested target: "
        + _fmt_price(payload.target)
        + " "
        + escape_md(f"({payload.target - payload.stop:.2f}R)"),
    ]
    return "\n".join(lines)


def format_system(text: str) -> str:
    return escape_md(f"⚠️ {text}")


def format_startup(text: str) -> str:
    return escape_md(f"🤖 {text}")
