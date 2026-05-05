from oilbot.config import Config
from oilbot.persistence.db import DEFAULT_DB_PATH, Database

_SENTIMENT_LABEL = {"BULLISH": "🟢 BULLISH", "BEARISH": "🔴 BEARISH", "NEUTRAL": "⬜ NEUTRAL"}


async def status(config: Config, verbose: bool = False, limit: int = 10) -> None:
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

    if verbose:
        rows = db.recent_classifications(limit)
        print(f"\nLast {limit} classified posts:")
        if not rows:
            print("  (none)")
        else:
            for row in rows:
                ts = row["classified_at"][:19].replace("T", " ")
                source = row["source"] or "unknown"
                label = _SENTIMENT_LABEL.get(row["sentiment"], row["sentiment"])
                conf = float(row["confidence"])
                text = row["text"].replace("\n", " ")[:120]
                if len(row["text"]) > 120:
                    text += "…"
                print(f"  [{ts}] {source} | {label} ({conf:.2f}) | {text}")
