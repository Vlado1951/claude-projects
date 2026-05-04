from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass
class Bar:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class PriceWindow:
    high: float
    low: float
    open: float
    close: float
    bars: list[Bar]


class PriceSource(Protocol):
    """Structural interface for price monitors — decouples the engine from ib_insync."""

    @property
    def is_connected(self) -> bool: ...

    @property
    def last_price(self) -> float | None: ...

    def get_window(self, end: datetime, lookback_seconds: int) -> PriceWindow: ...
