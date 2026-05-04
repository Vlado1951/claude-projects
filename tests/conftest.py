import asyncio

# eventkit (ib_insync dep) calls asyncio.get_event_loop() at module import time.
# Python 3.10+ no longer auto-creates a loop outside a running coroutine, so we
# set one here — before pytest imports any test file that transitively loads ib_insync.
asyncio.set_event_loop(asyncio.new_event_loop())
