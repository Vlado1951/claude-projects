from datetime import UTC, datetime, timedelta
from pathlib import Path

from oilbot.persistence.db import Database


def _db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


# ── schema / construction ─────────────────────────────────────────────────────

def test_init_creates_db_file(tmp_path: Path):
    _db(tmp_path)
    assert (tmp_path / "test.db").exists()


def test_init_schema_idempotent(tmp_path: Path):
    db = _db(tmp_path)
    db.init_schema()  # should not raise on second call


# ── mark_seen / is_seen ───────────────────────────────────────────────────────

def test_is_seen_false_for_unknown(tmp_path: Path):
    db = _db(tmp_path)
    assert db.is_seen("no-such-post") is False


def test_mark_seen_then_is_seen(tmp_path: Path):
    db = _db(tmp_path)
    db.mark_seen("post-1")
    assert db.is_seen("post-1") is True


def test_mark_seen_idempotent(tmp_path: Path):
    db = _db(tmp_path)
    db.mark_seen("post-1")
    db.mark_seen("post-1")  # should not raise
    assert db.is_seen("post-1") is True


def test_is_seen_only_matches_exact_id(tmp_path: Path):
    db = _db(tmp_path)
    db.mark_seen("post-1")
    assert db.is_seen("post-2") is False


# ── record_classification ─────────────────────────────────────────────────────

def test_record_classification_stores_data(tmp_path: Path):
    db = _db(tmp_path)
    db.record_classification(
        post_id="p1",
        sentiment="BULLISH",
        confidence=0.9,
        rationale="test",
        source="keywords",
    )
    s = db.last_24h_summary()
    assert s["classified_bullish"] == 1


def test_record_classification_upserts(tmp_path: Path):
    db = _db(tmp_path)
    db.record_classification("p1", "BULLISH", 0.9, "first", "keywords")
    db.record_classification("p1", "BEARISH", 0.8, "second", "llm")
    s = db.last_24h_summary()
    assert s["classified_bullish"] == 0
    assert s["classified_bearish"] == 1


# ── record_alert ──────────────────────────────────────────────────────────────

def test_record_alert_stores_data(tmp_path: Path):
    db = _db(tmp_path)
    db.record_alert(post_id="p1", side="BUY", entry=78.45, stop=78.21, target=78.81)
    s = db.last_24h_summary()
    assert s["alerts_sent"] == 1


def test_record_alert_allows_multiple_per_post(tmp_path: Path):
    db = _db(tmp_path)
    db.record_alert("p1", "BUY", 78.0, 77.8, 78.4)
    db.record_alert("p1", "BUY", 78.1, 77.9, 78.5)
    s = db.last_24h_summary()
    assert s["alerts_sent"] == 2


# ── last_24h_summary ──────────────────────────────────────────────────────────

def test_summary_zero_on_fresh_db(tmp_path: Path):
    db = _db(tmp_path)
    s = db.last_24h_summary()
    assert s == {
        "posts_seen": 0,
        "classified_bullish": 0,
        "classified_bearish": 0,
        "classified_neutral": 0,
        "alerts_sent": 0,
    }


def test_summary_counts_all_sentiments(tmp_path: Path):
    db = _db(tmp_path)
    db.record_classification("p1", "BULLISH", 0.9, "r", "keywords")
    db.record_classification("p2", "BULLISH", 0.8, "r", "keywords")
    db.record_classification("p3", "BEARISH", 0.7, "r", "keywords")
    db.record_classification("p4", "NEUTRAL", 0.5, "r", "keywords")
    s = db.last_24h_summary()
    assert s["classified_bullish"] == 2
    assert s["classified_bearish"] == 1
    assert s["classified_neutral"] == 1


def test_summary_excludes_old_classifications(tmp_path: Path):
    db = _db(tmp_path)
    old_ts = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    db.record_classification("p1", "BULLISH", 0.9, "r", "keywords", classified_at=old_ts)
    s = db.last_24h_summary()
    assert s["classified_bullish"] == 0


def test_summary_counts_posts_seen(tmp_path: Path):
    db = _db(tmp_path)
    db.mark_seen("p1")
    db.mark_seen("p2")
    s = db.last_24h_summary()
    assert s["posts_seen"] == 2


# ── per_source_events / per_source_alerts ─────────────────────────────────────

def test_per_source_events_empty_on_fresh_db(tmp_path: Path):
    assert _db(tmp_path).per_source_events() == {}


def test_per_source_events_groups_by_source(tmp_path: Path):
    db = _db(tmp_path)
    db.mark_seen("p1", "truth_social")
    db.mark_seen("p2", "truth_social")
    db.mark_seen("p3", "google_news_oil")
    counts = db.per_source_events()
    assert counts["truth_social"] == 2
    assert counts["google_news_oil"] == 1


def test_per_source_alerts_empty_on_fresh_db(tmp_path: Path):
    assert _db(tmp_path).per_source_alerts() == {}


def test_per_source_alerts_groups_by_source(tmp_path: Path):
    db = _db(tmp_path)
    db.mark_seen("p1", "truth_social")
    db.mark_seen("p2", "google_news_oil")
    db.record_alert("p1", "BUY", 100.0, 99.0, 101.5)
    db.record_alert("p2", "BUY", 100.0, 99.0, 101.5)
    counts = db.per_source_alerts()
    assert counts.get("truth_social", 0) == 1
    assert counts.get("google_news_oil", 0) == 1
