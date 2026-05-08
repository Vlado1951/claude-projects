"""Unit tests for OutcomesStore (Phase 18)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oilbot.persistence.db import Database
from oilbot.persistence.outcomes import OutcomesStore


def _store(tmp_path: Path) -> tuple[OutcomesStore, Database]:
    db = Database(tmp_path / "test.db")
    store = OutcomesStore(tmp_path / "test.db")
    return store, db


def _alert_id(db: Database, post_id: str = "post-1") -> int:
    return db.record_alert(
        post_id=post_id,
        side="BUY",
        entry=80.0,
        stop=79.0,
        target=81.5,
    )


# ---------------------------------------------------------------------------
# open_outcome
# ---------------------------------------------------------------------------


def test_open_outcome_creates_tracking_row(tmp_path: Path) -> None:
    store, db = _store(tmp_path)
    aid = _alert_id(db)
    store.open_outcome(aid)
    row = store.get_by_alert(aid)
    assert row is not None
    assert row["status"] == "tracking"
    assert row["alert_id"] == aid


def test_open_outcome_idempotent(tmp_path: Path) -> None:
    store, db = _store(tmp_path)
    aid = _alert_id(db)
    store.open_outcome(aid)
    store.open_outcome(aid)  # second call must not raise or create a duplicate
    assert len(store.list_open()) == 1


def test_open_outcome_records_tracked_from_at(tmp_path: Path) -> None:
    store, db = _store(tmp_path)
    aid = _alert_id(db)
    t = datetime(2024, 6, 1, 12, 0, 0, tzinfo=UTC)
    store.open_outcome(aid, tracked_from_at=t)
    row = store.get_by_alert(aid)
    assert row is not None
    assert row["tracked_from_at"].startswith("2024-06-01T12:00:00")


# ---------------------------------------------------------------------------
# list_open / list_closed
# ---------------------------------------------------------------------------


def test_list_open_returns_only_tracking(tmp_path: Path) -> None:
    store, db = _store(tmp_path)
    aid1 = _alert_id(db, "post-1")
    aid2 = _alert_id(db, "post-2")
    store.open_outcome(aid1)
    store.open_outcome(aid2)
    store.close_outcome(aid2, status="target_hit", realized_r=1.5)
    open_ids = {r["alert_id"] for r in store.list_open()}
    assert open_ids == {aid1}


def test_list_closed_returns_only_closed(tmp_path: Path) -> None:
    store, db = _store(tmp_path)
    aid1 = _alert_id(db, "post-1")
    aid2 = _alert_id(db, "post-2")
    store.open_outcome(aid1)
    store.open_outcome(aid2)
    store.close_outcome(aid2, status="stop_hit", realized_r=-1.0)
    closed = store.list_closed()
    assert len(closed) == 1
    assert closed[0]["alert_id"] == aid2


def test_list_closed_since_filters_by_date(tmp_path: Path) -> None:
    store, db = _store(tmp_path)
    old = datetime(2024, 1, 1, tzinfo=UTC)
    recent = datetime(2024, 6, 1, tzinfo=UTC)

    aid1 = _alert_id(db, "post-1")
    aid2 = _alert_id(db, "post-2")
    store.open_outcome(aid1, tracked_from_at=old)
    store.open_outcome(aid2, tracked_from_at=recent)
    store.close_outcome(aid1, status="timeout")
    store.close_outcome(aid2, status="target_hit", realized_r=1.0)

    cutoff = datetime(2024, 3, 1, tzinfo=UTC)
    closed = store.list_closed(since=cutoff)
    assert len(closed) == 1
    assert closed[0]["alert_id"] == aid2


# ---------------------------------------------------------------------------
# close_outcome
# ---------------------------------------------------------------------------


def test_close_outcome_sets_all_fields(tmp_path: Path) -> None:
    store, db = _store(tmp_path)
    aid = _alert_id(db)
    store.open_outcome(aid)

    t_exit = datetime(2024, 6, 1, 13, 0, 0, tzinfo=UTC)
    store.close_outcome(
        aid,
        status="target_hit",
        exit_price=81.5,
        exit_at=t_exit,
        realized_r=1.5,
        mfe_r=1.62,
        mae_r=-0.34,
        mfe_price=81.62,
        mae_price=79.66,
    )

    row = store.get_by_alert(aid)
    assert row is not None
    assert row["status"] == "target_hit"
    assert row["exit_price"] == pytest.approx(81.5)
    assert row["realized_r"] == pytest.approx(1.5)
    assert row["mfe_r"] == pytest.approx(1.62)
    assert row["mae_r"] == pytest.approx(-0.34)
    assert row["tracked_until_at"] is not None


def test_close_outcome_stop_hit(tmp_path: Path) -> None:
    store, db = _store(tmp_path)
    aid = _alert_id(db)
    store.open_outcome(aid)
    store.close_outcome(aid, status="stop_hit", realized_r=-1.0)
    row = store.get_by_alert(aid)
    assert row is not None
    assert row["status"] == "stop_hit"
    assert row["realized_r"] == pytest.approx(-1.0)


def test_close_outcome_timeout_no_r(tmp_path: Path) -> None:
    store, db = _store(tmp_path)
    aid = _alert_id(db)
    store.open_outcome(aid)
    store.close_outcome(aid, status="timeout")
    row = store.get_by_alert(aid)
    assert row is not None
    assert row["status"] == "timeout"
    assert row["realized_r"] is None


# ---------------------------------------------------------------------------
# update_outcome
# ---------------------------------------------------------------------------


def test_update_outcome_partial_fields(tmp_path: Path) -> None:
    store, db = _store(tmp_path)
    aid = _alert_id(db)
    store.open_outcome(aid)
    store.update_outcome(aid, fill_price=80.05, mfe_price=80.10)
    row = store.get_by_alert(aid)
    assert row is not None
    assert row["fill_price"] == pytest.approx(80.05)
    assert row["mfe_price"] == pytest.approx(80.10)
    assert row["status"] == "tracking"  # unchanged


def test_update_outcome_noop_when_no_fields(tmp_path: Path) -> None:
    store, db = _store(tmp_path)
    aid = _alert_id(db)
    store.open_outcome(aid)
    store.update_outcome(aid)  # must not raise
    assert store.get_by_alert(aid) is not None


# ---------------------------------------------------------------------------
# get_by_alert
# ---------------------------------------------------------------------------


def test_get_by_alert_returns_none_for_missing(tmp_path: Path) -> None:
    store, db = _store(tmp_path)
    assert store.get_by_alert(9999) is None


# ---------------------------------------------------------------------------
# uniqueness — alert_id is UNIQUE
# ---------------------------------------------------------------------------


def test_alert_outcomes_unique_per_alert(tmp_path: Path) -> None:
    """Two open_outcome calls on the same alert_id must not produce two rows."""
    store, db = _store(tmp_path)
    aid = _alert_id(db)
    store.open_outcome(aid)
    store.open_outcome(aid)
    assert len(store.list_open()) == 1


# ---------------------------------------------------------------------------
# record_alert returns alert id
# ---------------------------------------------------------------------------


def test_record_alert_returns_incrementing_ids(tmp_path: Path) -> None:
    _, db = _store(tmp_path)
    id1 = db.record_alert("p1", "BUY", 80.0, 79.0, 81.5)
    id2 = db.record_alert("p2", "SELL", 80.0, 81.0, 78.5)
    assert isinstance(id1, int)
    assert id2 > id1
