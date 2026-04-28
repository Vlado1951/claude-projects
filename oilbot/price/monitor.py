from dataclasses import dataclass
from datetime import datetime


@dataclass
class PriceWindow:
    high: float
    low: float
    open: float
    close: float
    bars: list[object]


class PriceMonitor:
    async def start(self) -> None:
        raise NotImplementedError

    def get_window(self, end: datetime, lookback_seconds: int) -> PriceWindow:
        raise NotImplementedError

    @property
    def last_price(self) -> float | None:
        return None

    @property
    def is_connected(self) -> bool:
        return False
