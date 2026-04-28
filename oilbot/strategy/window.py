from oilbot.price.monitor import PriceWindow


def find_spike_high(window: PriceWindow) -> float:
    raise NotImplementedError


def detect_pullback(bars: list[object], spike_high: float, atr: float, k: float) -> float | None:
    raise NotImplementedError


def detect_breakout(bars: list[object], spike_high: float) -> float | None:
    raise NotImplementedError
