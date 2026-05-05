from __future__ import annotations

import structlog

from oilbot.config import Config
from oilbot.news.configured_sources import EIA_SOURCES

log = structlog.get_logger()


async def test_eia(config: Config, source_name: str) -> None:
    from oilbot.classifier.hybrid import HybridClassifier
    from oilbot.news.eia import EIAReleaseListener

    if source_name not in EIA_SOURCES:
        names = ", ".join(EIA_SOURCES)
        print(f"Unknown EIA source '{source_name}'. Available: {names}")
        return

    if not config.eia_api_key:
        print("EIA_API_KEY is not set in .env")
        return

    ds = EIA_SOURCES[source_name]
    listener = EIAReleaseListener(api_key=config.eia_api_key, dataset=ds)
    classifier = HybridClassifier(config)

    print(f"Fetching latest {ds.metric} data from EIA…")
    result = await listener.fetch_latest()
    if result is None:
        print("No data returned (API returned fewer than 2 rows).")
        return

    period, delta = result
    event = listener.make_event(period, delta)
    cls = await classifier.classify(event)

    direction = "draw" if delta < 0 else "build"
    print(
        f"\n  Source   : {source_name}"
        f"\n  Period   : {period}"
        f"\n  Metric   : {ds.metric}"
        f"\n  Delta    : {direction} of {abs(delta):.3f} {ds.unit}"
        f"\n  Threshold: ±"
        f"{config.eia_crude_threshold_mbbl if ds.unit == 'mbbl' else config.eia_rig_threshold}"
        f" {ds.unit}"
        f"\n  Class    : {cls.sentiment.value} ({cls.source}, conf={cls.confidence:.2f})"
        f"\n  Rationale: {cls.rationale}"
    )
