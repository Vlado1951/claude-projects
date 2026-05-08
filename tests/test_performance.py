"""Unit tests for performance CLI (Phase 20)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oilbot.cli.performance import (
    compute_stats,
    fetch_rows,
    fetch_total_and_tracking,
    format_stats_block,
    group_by,
    parse_since,
)
from oilbot.persistence.db import Database
from oilbot.persistence.outcomes import OutcomesStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_db(tmp_path: Path, outcomes: list[dict]) -> Path:
    """Insert alerts + outcomes into a fresh DB and return db_path."""
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    store = OutcomesStore(db_path)
    for o in outcomes:
        aid = db.record_alert(
            o.get("post_id", "p"),
            o.get("side", "BUY"),
            o.get("entry", 80.0),
            o.get("stop", 79.0),
            o.get("target", 81.5),
            confirmation_status=o.get("confirmation_status", "not_checked"),
        )
        # Also insert a seen_posts row so source joins work
        db.mark_seen(
            o.get("post_id", "p"),
            source=o.get("source", "truth_social"),
            raw_text="test",
        )
        # Insert a classification row so sentiment joins work
        db.record_classification(
            o.get("post_id", "p"),
            sentiment=o.get("sentiment", "BULLISH"),
            confidence=1.0,
            rationale="test",
            source="keywords",
        )
        from_dt = o.get("tracked_from", datetime.now(UTC))
        store.open_outcome(aid, tracked_from_at=from_dt)
        store.close_outcome(
            aid,
            status=o["status"],
            realized_r=o.get("realized_r"),
            mfe_r=o.get("mfe_r"),
            mae_r=o.get("mae_r"),
            tracked_until_at=from_dt + timedelta(minutes=10),
        )
    return db_path


# Five outcomes: 3 target_hit, 1 stop_hit, 1 timeout
_FIVE_OUTCOMES = [
    {"post_id": "p1", "status": "target_hit", "realized_r": 1.5, "mfe_r": 1.62, "mae_r": -0.10},
    {"post_id": "p2", "status": "target_hit", "realized_r": 1.2, "mfe_r": 1.30, "mae_r": -0.05},
    {"post_id": "p3", "status": "target_hit", "realized_r": 0.8, "mfe_r": 0.90, "mae_r": -0.20},
    {"post_id": "p4", "status": "stop_hit",   "realized_r": -1.0, "mfe_r": 0.30, "mae_r": -1.0},
    {"post_id": "p5", "status": "timeout",    "realized_r": 0.0,  "mfe_r": 0.40, "mae_r": -0.30},
]


# ---------------------------------------------------------------------------
# parse_since
# ---------------------------------------------------------------------------


def test_parse_since_days() -> None:
    before = datetime.now(UTC)
    result = parse_since("7d")
    assert (before - result).total_seconds() == pytest.approx(7 * 86400, abs=5)


def test_parse_since_hours() -> None:
    before = datetime.now(UTC)
    result = parse_since("24h")
    assert (before - result).total_seconds() == pytest.approx(24 * 3600, abs=5)


def test_parse_since_weeks() -> None:
    before = datetime.now(UTC)
    result = parse_since("2w")
    assert (before - result).total_seconds() == pytest.approx(2 * 7 * 86400, abs=5)


def test_parse_since_invalid_unit() -> None:
    with pytest.raises(ValueError, match="Unknown unit"):
        parse_since("3y")


def test_parse_since_invalid_format() -> None:
    with pytest.raises(ValueError):
        parse_since("abcd")


# ---------------------------------------------------------------------------
# compute_stats
# ---------------------------------------------------------------------------


def test_compute_stats_empty() -> None:
    stats = compute_stats([])
    assert stats["total"] == 0
    assert not stats["sufficient"]


def test_compute_stats_below_threshold() -> None:
    rows = [{"status": "target_hit", "realized_r": 1.5, "mfe_r": 1.6, "mae_r": -0.1}]
    stats = compute_stats(rows)
    assert stats["total"] == 1
    assert not stats["sufficient"]


def test_compute_stats_counts(tmp_path: Path) -> None:
    rows = fetch_rows(_seed_db(tmp_path, _FIVE_OUTCOMES))
    stats = compute_stats(rows)
    assert stats["total"] == 5
    assert stats["target_hit"] == 3
    assert stats["stop_hit"] == 1
    assert stats["timeout"] == 1
    assert stats["sufficient"]


def test_compute_stats_rates(tmp_path: Path) -> None:
    rows = fetch_rows(_seed_db(tmp_path, _FIVE_OUTCOMES))
    stats = compute_stats(rows)
    assert stats["hit_rate"] == pytest.approx(0.6, abs=0.01)
    assert stats["stop_rate"] == pytest.approx(0.2, abs=0.01)
    assert stats["timeout_rate"] == pytest.approx(0.2, abs=0.01)


def test_compute_stats_avg_r(tmp_path: Path) -> None:
    rows = fetch_rows(_seed_db(tmp_path, _FIVE_OUTCOMES))
    stats = compute_stats(rows)
    # (1.5 + 1.2 + 0.8 - 1.0 + 0.0) / 5 = 0.5
    assert stats["avg_r"] == pytest.approx(0.5, abs=0.01)


def test_compute_stats_median_r(tmp_path: Path) -> None:
    rows = fetch_rows(_seed_db(tmp_path, _FIVE_OUTCOMES))
    stats = compute_stats(rows)
    # sorted: -1.0, 0.0, 0.8, 1.2, 1.5 → median = 0.8
    assert stats["median_r"] == pytest.approx(0.8, abs=0.01)


def test_compute_stats_profit_factor(tmp_path: Path) -> None:
    rows = fetch_rows(_seed_db(tmp_path, _FIVE_OUTCOMES))
    stats = compute_stats(rows)
    # winners: 1.5 + 1.2 + 0.8 + 0.0 = 3.5  (0.0 timeout is not a "winner" since not > 0)
    # winners > 0: 1.5 + 1.2 + 0.8 = 3.5; losers < 0: -1.0 → pf = 3.5 / 1.0 = 3.5
    assert stats["profit_factor"] == pytest.approx(3.5, abs=0.01)


def test_compute_stats_best_worst(tmp_path: Path) -> None:
    rows = fetch_rows(_seed_db(tmp_path, _FIVE_OUTCOMES))
    stats = compute_stats(rows)
    assert stats["best_r"] == pytest.approx(1.5, abs=0.01)
    assert stats["worst_r"] == pytest.approx(-1.0, abs=0.01)


def test_compute_stats_all_winners_profit_factor() -> None:
    rows = [
        {"status": "target_hit", "realized_r": 1.5, "mfe_r": 1.6, "mae_r": -0.1},
        {"status": "target_hit", "realized_r": 1.0, "mfe_r": 1.1, "mae_r": -0.1},
        {"status": "target_hit", "realized_r": 0.8, "mfe_r": 0.9, "mae_r": -0.1},
        {"status": "target_hit", "realized_r": 0.5, "mfe_r": 0.6, "mae_r": -0.1},
        {"status": "target_hit", "realized_r": 0.3, "mfe_r": 0.4, "mae_r": -0.1},
    ]
    stats = compute_stats(rows)
    assert stats["profit_factor"] == float("inf")


def test_compute_stats_all_losers_profit_factor() -> None:
    rows = [
        {"status": "stop_hit", "realized_r": -1.0, "mfe_r": 0.2, "mae_r": -1.0},
        {"status": "stop_hit", "realized_r": -1.0, "mfe_r": 0.1, "mae_r": -1.0},
        {"status": "stop_hit", "realized_r": -0.8, "mfe_r": 0.0, "mae_r": -0.8},
        {"status": "stop_hit", "realized_r": -1.0, "mfe_r": 0.2, "mae_r": -1.0},
        {"status": "stop_hit", "realized_r": -0.9, "mfe_r": 0.1, "mae_r": -0.9},
    ]
    stats = compute_stats(rows)
    assert stats["profit_factor"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# fetch_rows + since filter
# ---------------------------------------------------------------------------


def test_fetch_rows_since_filters_old_outcomes(tmp_path: Path) -> None:
    recent_dt = datetime.now(UTC) - timedelta(hours=1)
    old_dt = datetime.now(UTC) - timedelta(days=10)
    outcomes = [
        {"post_id": "recent", "status": "target_hit", "realized_r": 1.5,
         "tracked_from": recent_dt},
        {"post_id": "old",    "status": "target_hit", "realized_r": 1.0,
         "tracked_from": old_dt},
    ]
    db_path = _seed_db(tmp_path, outcomes)
    since = datetime.now(UTC) - timedelta(days=2)
    rows = fetch_rows(db_path, since=since)
    assert len(rows) == 1
    assert rows[0]["realized_r"] == pytest.approx(1.5)


def test_fetch_rows_excludes_tracking(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db")
    store = OutcomesStore(tmp_path / "test.db")
    aid = db.record_alert("p-t", "BUY", 80.0, 79.0, 81.5)
    store.open_outcome(aid)  # status = tracking, never closed

    rows = fetch_rows(tmp_path / "test.db")
    assert all(r["alert_id"] != aid for r in rows)


# ---------------------------------------------------------------------------
# fetch_total_and_tracking
# ---------------------------------------------------------------------------


def test_fetch_total_and_tracking(tmp_path: Path) -> None:
    db_path = _seed_db(tmp_path, _FIVE_OUTCOMES)
    # Add one still-tracking row
    db = Database(db_path)
    store = OutcomesStore(db_path)
    aid = db.record_alert("p-live", "BUY", 80.0, 79.0, 81.5)
    store.open_outcome(aid)

    total, tracking = fetch_total_and_tracking(db_path)
    assert total == 6
    assert tracking == 1


# ---------------------------------------------------------------------------
# group_by
# ---------------------------------------------------------------------------


def test_group_by_source(tmp_path: Path) -> None:
    outcomes = [
        {"post_id": "a", "source": "truth_social",  "status": "target_hit", "realized_r": 1.5},
        {"post_id": "b", "source": "truth_social",  "status": "stop_hit",   "realized_r": -1.0},
        {"post_id": "c", "source": "eia_petroleum", "status": "target_hit", "realized_r": 0.8},
        {"post_id": "d", "source": "eia_petroleum", "status": "target_hit", "realized_r": 1.2},
        {"post_id": "e", "source": "eia_petroleum", "status": "timeout",    "realized_r": 0.0},
    ]
    rows = fetch_rows(_seed_db(tmp_path, outcomes))
    groups = group_by(rows, "source")
    assert set(groups.keys()) == {"truth_social", "eia_petroleum"}
    assert len(groups["truth_social"]) == 2
    assert len(groups["eia_petroleum"]) == 3


def test_group_by_confirmation(tmp_path: Path) -> None:
    outcomes = [
        {"post_id": "a", "confirmation_status": "aligned",    "status": "target_hit", "realized_r": 1.5},
        {"post_id": "b", "confirmation_status": "aligned",    "status": "target_hit", "realized_r": 1.2},
        {"post_id": "c", "confirmation_status": "disaligned", "status": "stop_hit",   "realized_r": -1.0},
        {"post_id": "d", "confirmation_status": "mixed",      "status": "target_hit", "realized_r": 0.8},
        {"post_id": "e", "confirmation_status": "aligned",    "status": "timeout",    "realized_r": 0.0},
    ]
    rows = fetch_rows(_seed_db(tmp_path, outcomes))
    groups = group_by(rows, "confirmation")
    assert len(groups["aligned"]) == 3
    assert len(groups["disaligned"]) == 1


def test_group_by_sentiment(tmp_path: Path) -> None:
    outcomes = [
        {"post_id": "a", "sentiment": "BULLISH", "status": "target_hit", "realized_r": 1.5},
        {"post_id": "b", "sentiment": "BEARISH", "status": "stop_hit",   "realized_r": -1.0},
        {"post_id": "c", "sentiment": "BULLISH", "status": "target_hit", "realized_r": 0.8},
        {"post_id": "d", "sentiment": "BULLISH", "status": "target_hit", "realized_r": 1.2},
        {"post_id": "e", "sentiment": "BEARISH", "status": "timeout",    "realized_r": 0.0},
    ]
    rows = fetch_rows(_seed_db(tmp_path, outcomes))
    groups = group_by(rows, "sentiment")
    assert len(groups["BULLISH"]) == 3
    assert len(groups["BEARISH"]) == 2


# ---------------------------------------------------------------------------
# format_stats_block
# ---------------------------------------------------------------------------


def test_format_stats_block_insufficient_sample(tmp_path: Path) -> None:
    rows = fetch_rows(_seed_db(tmp_path, _FIVE_OUTCOMES[:3]))
    out = format_stats_block(compute_stats(rows))
    assert "insufficient sample size" in out


def test_format_stats_block_no_alerts() -> None:
    out = format_stats_block(compute_stats([]))
    assert "No closed alerts" in out


def test_format_stats_block_contains_key_metrics(tmp_path: Path) -> None:
    rows = fetch_rows(_seed_db(tmp_path, _FIVE_OUTCOMES))
    out = format_stats_block(compute_stats(rows))
    assert "60.0%" in out        # hit rate
    assert "Avg R" in out
    assert "Profit factor" in out
    assert "Best" in out
    assert "Worst" in out
