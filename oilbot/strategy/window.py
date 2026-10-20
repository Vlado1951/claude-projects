from oilbot.price.types import Bar


def find_spike_high(bars: list[Bar]) -> float:
    return max(b.close for b in bars)


def find_dump_low(bars: list[Bar]) -> float:
    return min(b.close for b in bars)


def compute_atr(bars: list[Bar], n: int = 12) -> float | None:
    if len(bars) < 2:
        return None
    recent = bars[-n:]
    trs = [
        max(
            recent[i].high - recent[i].low,
            abs(recent[i].high - recent[i - 1].close),
            abs(recent[i].low - recent[i - 1].close),
        )
        for i in range(1, len(recent))
    ]
    return sum(trs) / len(trs) if trs else None


def detect_pullback(bars: list[Bar], spike_high: float, threshold: float) -> float | None:
    """Returns pullback low if a valid pullback below spike_high is found.

    threshold = k * ATR (or k_static fallback from config).
    """
    # Method 1: close below spike_high - threshold
    for i, bar in enumerate(bars):
        if bar.close < spike_high - threshold:
            return min(b.close for b in bars[i:])
    # Method 2: 3-bar lower-lows pattern
    for i in range(2, len(bars)):
        if bars[i].low < bars[i - 1].low < bars[i - 2].low:
            return min(b.close for b in bars[i:])
    return None


def detect_bounce(bars: list[Bar], dump_low: float, threshold: float) -> float | None:
    """Bearish mirror of detect_pullback. Returns bounce high if found."""
    # Method 1: close above dump_low + threshold
    for i, bar in enumerate(bars):
        if bar.close > dump_low + threshold:
            return max(b.close for b in bars[i:])
    # Method 2: 3-bar higher-highs pattern
    for i in range(2, len(bars)):
        if bars[i].high > bars[i - 1].high > bars[i - 2].high:
            return max(b.close for b in bars[i:])
    return None


def detect_breakout(bars: list[Bar], spike_high: float) -> float | None:
    """Returns close of first bar that closes above spike_high, else None."""
    for bar in bars:
        if bar.close > spike_high:
            return bar.close
    return None


def detect_breakdown(bars: list[Bar], dump_low: float) -> float | None:
    """Returns close of first bar that closes below dump_low, else None."""
    for bar in bars:
        if bar.close < dump_low:
            return bar.close
    return None
