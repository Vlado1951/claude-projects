import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from oilbot.types import Classification, Sentiment

DEFAULT_DB_PATH = Path("data/bot.db")


class ClassificationCache:
    """Lightweight cache used by HybridClassifier to avoid re-billing LLM calls."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        self._init_schema()

    def _init_schema(self) -> None:
        with sqlite3.connect(self._path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS classifications (
                    post_id       TEXT PRIMARY KEY,
                    sentiment     TEXT NOT NULL,
                    confidence    REAL NOT NULL,
                    rationale     TEXT NOT NULL,
                    source        TEXT NOT NULL,
                    classified_at TEXT NOT NULL
                )
            """)

    def get(self, post_id: str) -> Classification | None:
        with sqlite3.connect(self._path) as conn:
            row = conn.execute(
                "SELECT sentiment, confidence, rationale FROM classifications WHERE post_id = ?",
                (post_id,),
            ).fetchone()
        if row is None:
            return None
        return Classification(
            sentiment=Sentiment(row[0]),
            confidence=row[1],
            rationale=row[2],
            source="cache",
        )

    def save(self, post_id: str, c: Classification) -> None:
        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO classifications
                   (post_id, sentiment, confidence, rationale, source, classified_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (post_id, c.sentiment.value, c.confidence, c.rationale, c.source, now),
            )


class Database:
    """Full audit-trail and dedupe store. Owns all three tables."""

    def __init__(self, db_path: Path = DEFAULT_DB_PATH) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._path = db_path
        self.init_schema()

    def init_schema(self) -> None:
        with sqlite3.connect(self._path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS seen_posts (
                    id          TEXT NOT NULL,
                    source      TEXT NOT NULL DEFAULT 'truth_social',
                    author      TEXT NOT NULL DEFAULT '',
                    posted_at   TEXT NOT NULL DEFAULT '',
                    raw_text    TEXT NOT NULL DEFAULT '',
                    inserted_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (id, source)
                )
            """)
            self._migrate_seen_posts_source(conn)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS classifications (
                    post_id       TEXT PRIMARY KEY,
                    sentiment     TEXT NOT NULL,
                    confidence    REAL NOT NULL,
                    rationale     TEXT NOT NULL,
                    source        TEXT NOT NULL,
                    classified_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id  TEXT NOT NULL,
                    side     TEXT NOT NULL,
                    entry    REAL NOT NULL,
                    stop     REAL NOT NULL,
                    target   REAL NOT NULL,
                    sent_at  TEXT NOT NULL
                )
            """)

    def _migrate_seen_posts_source(self, conn: sqlite3.Connection) -> None:
        """One-time migration: rebuild seen_posts with (id, source) primary key."""
        cols = {row[1] for row in conn.execute("PRAGMA table_info(seen_posts)")}
        if "source" not in cols:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS seen_posts_new (
                    id          TEXT NOT NULL,
                    source      TEXT NOT NULL DEFAULT 'truth_social',
                    author      TEXT NOT NULL DEFAULT '',
                    posted_at   TEXT NOT NULL DEFAULT '',
                    raw_text    TEXT NOT NULL DEFAULT '',
                    inserted_at TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (id, source)
                );
                INSERT INTO seen_posts_new (id, source, author, posted_at, raw_text, inserted_at)
                SELECT id, 'truth_social', author, posted_at, raw_text, inserted_at
                FROM seen_posts;
                DROP TABLE seen_posts;
                ALTER TABLE seen_posts_new RENAME TO seen_posts;
            """)

    def mark_seen(self, post_id: str, source: str = "truth_social") -> None:
        now = datetime.now(UTC).isoformat()
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO seen_posts (id, source, inserted_at) VALUES (?, ?, ?)",
                (post_id, source, now),
            )

    def is_seen(self, post_id: str, source: str = "truth_social") -> bool:
        with sqlite3.connect(self._path) as conn:
            row = conn.execute(
                "SELECT 1 FROM seen_posts WHERE id = ? AND source = ?", (post_id, source)
            ).fetchone()
        return row is not None

    def record_classification(
        self,
        post_id: str,
        sentiment: str,
        confidence: float,
        rationale: str,
        source: str,
        classified_at: str | None = None,
    ) -> None:
        now = classified_at or datetime.now(UTC).isoformat()
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO classifications
                   (post_id, sentiment, confidence, rationale, source, classified_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (post_id, sentiment, confidence, rationale, source, now),
            )

    def record_alert(
        self,
        post_id: str,
        side: str,
        entry: float,
        stop: float,
        target: float,
        sent_at: str | None = None,
    ) -> None:
        now = sent_at or datetime.now(UTC).isoformat()
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                """INSERT INTO alerts (post_id, side, entry, stop, target, sent_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (post_id, side, entry, stop, target, now),
            )

    def last_24h_summary(self) -> dict[str, int]:
        cutoff = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
        with sqlite3.connect(self._path) as conn:
            posts_seen = conn.execute(
                "SELECT COUNT(*) FROM seen_posts WHERE inserted_at >= ?",
                (cutoff,),
            ).fetchone()[0]
            bullish = conn.execute(
                "SELECT COUNT(*) FROM classifications"
                " WHERE sentiment = 'BULLISH' AND classified_at >= ?",
                (cutoff,),
            ).fetchone()[0]
            bearish = conn.execute(
                "SELECT COUNT(*) FROM classifications"
                " WHERE sentiment = 'BEARISH' AND classified_at >= ?",
                (cutoff,),
            ).fetchone()[0]
            neutral = conn.execute(
                "SELECT COUNT(*) FROM classifications"
                " WHERE sentiment = 'NEUTRAL' AND classified_at >= ?",
                (cutoff,),
            ).fetchone()[0]
            alerts_sent = conn.execute(
                "SELECT COUNT(*) FROM alerts WHERE sent_at >= ?",
                (cutoff,),
            ).fetchone()[0]
        return {
            "posts_seen": posts_seen,
            "classified_bullish": bullish,
            "classified_bearish": bearish,
            "classified_neutral": neutral,
            "alerts_sent": alerts_sent,
        }

    def per_source_events(self, hours: int = 24) -> dict[str, int]:
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        with sqlite3.connect(self._path) as conn:
            rows = conn.execute(
                "SELECT source, COUNT(*) FROM seen_posts"
                " WHERE inserted_at >= ? GROUP BY source",
                (cutoff,),
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    def per_source_alerts(self, hours: int = 24) -> dict[str, int]:
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        with sqlite3.connect(self._path) as conn:
            rows = conn.execute(
                "SELECT sp.source, COUNT(*) FROM alerts a"
                " JOIN seen_posts sp ON sp.id = a.post_id"
                " WHERE a.sent_at >= ? GROUP BY sp.source",
                (cutoff,),
            ).fetchall()
        return {row[0]: row[1] for row in rows}
