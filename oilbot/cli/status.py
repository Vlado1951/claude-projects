from oilbot.config import Config
from oilbot.persistence.db import DEFAULT_DB_PATH, Database


async def status(config: Config) -> None:
    db = Database(DEFAULT_DB_PATH)
    s = db.last_24h_summary()
    print("Last 24h summary")
    print(f"  Posts seen:          {s['posts_seen']}")
    print(f"  Classified bullish:  {s['classified_bullish']}")
    print(f"  Classified bearish:  {s['classified_bearish']}")
    print(f"  Classified neutral:  {s['classified_neutral']}")
    print(f"  Alerts sent:         {s['alerts_sent']}")

    events_by_source = db.per_source_events()
    alerts_by_source = db.per_source_alerts()
    if events_by_source or alerts_by_source:
        print("\nPer-source (last 24h):")
        for source in sorted(set(events_by_source) | set(alerts_by_source)):
            ev = events_by_source.get(source, 0)
            al = alerts_by_source.get(source, 0)
            print(f"  {source}: {ev} events seen, {al} alerts sent")
