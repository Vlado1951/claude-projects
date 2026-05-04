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
