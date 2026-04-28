import asyncio

import structlog

from oilbot.config import Config

log = structlog.get_logger()


async def run(config: Config) -> None:
    log.info(
        "starting",
        handle=config.truth_social_handle,
        ibkr_host=config.ibkr_host,
        ibkr_port=config.ibkr_port,
        llm_provider=config.llm_provider,
    )
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    log.info("shutdown")
