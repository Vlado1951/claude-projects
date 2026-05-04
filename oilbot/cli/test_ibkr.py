import asyncio

import structlog

from oilbot.config import Config
from oilbot.price.monitor import PriceMonitor

log = structlog.get_logger()


async def test_ibkr(config: Config) -> None:
    monitor = PriceMonitor(config)
    print(f"Connecting to TWS at {config.ibkr_host}:{config.ibkr_port} …")

    start_task = asyncio.create_task(monitor.start())

    # Wait up to 15s for connection and contract resolution
    deadline = 15.0
    step = 0.5
    elapsed = 0.0
    while not monitor.is_connected and elapsed < deadline:
        await asyncio.sleep(step)
        elapsed += step

    if not monitor.is_connected:
        start_task.cancel()
        try:
            await start_task
        except asyncio.CancelledError:
            pass
        print("ERROR: could not connect within 15 s. Is TWS/Gateway running?")
        return

    print("Connected. Streaming prices for 30 s …\n")

    updates = 0
    for _ in range(60):  # 60 × 0.5 s = 30 s
        await asyncio.sleep(0.5)
        price = monitor.last_price
        if price is not None:
            updates += 1
            print(f"  MCL last price: {price:.2f}", flush=True)

    start_task.cancel()
    try:
        await start_task
    except asyncio.CancelledError:
        pass

    print(f"\nDone. Received {updates} price update(s) in 30 s.")
    if updates < 5:
        print("WARNING: fewer than 5 updates — check your MCL market data subscription.")
