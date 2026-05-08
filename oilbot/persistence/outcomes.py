from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class OutcomesStore:
    """Thin persistence layer for alert_outcomes rows."""

    def __init__(self, db_path: Path) -> None:
        self._path = db_path

    def open_outcome(self, alert_id: int, tracked_from_at: datetime | None = None) -> None:
        now = (tracked_from_at or datetime.now(UTC)).isoformat()
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                "INSERT OR IGNORE INTO alert_outcomes (alert_id, status, tracked_from_at)"
                " VALUES (?, 'tracking', ?)",
                (alert_id, now),
            )

    def update_outcome(self, alert_id: int, **fields: Any) -> None:
        if not fields:
            return
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = [*fields.values(), alert_id]
        with sqlite3.connect(self._path) as conn:
            conn.execute(
                f"UPDATE alert_outcomes SET {set_clause} WHERE alert_id = ?",  # noqa: S608
                values,
            )

    def close_outcome(
        self,
        alert_id: int,
        *,
        status: str,
        exit_price: float | None = None,
        exit_at: datetime | None = None,
        realized_r: float | None = None,
        mfe_r: float | None = None,
        mae_r: float | None = None,
        mfe_price: float | None = None,
        mae_price: float | None = None,
        tracked_until_at: datetime | None = None,
    ) -> None:
        self.update_outcome(
            alert_id,
            status=status,
            exit_price=exit_price,
            exit_at=(exit_at.isoformat() if exit_at else None),
            realized_r=realized_r,
            mfe_r=mfe_r,
            mae_r=mae_r,
            mfe_price=mfe_price,
            mae_price=mae_price,
            tracked_until_at=(tracked_until_at or datetime.now(UTC)).isoformat(),
        )

    def list_open(self) -> list[dict[str, Any]]:
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM alert_outcomes WHERE status = 'tracking'"
            ).fetchall()
        return [dict(r) for r in rows]

    def list_closed(self, since: datetime | None = None) -> list[dict[str, Any]]:
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            if since:
                rows = conn.execute(
                    "SELECT * FROM alert_outcomes WHERE status != 'tracking'"
                    " AND tracked_from_at >= ?",
                    (since.isoformat(),),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM alert_outcomes WHERE status != 'tracking'"
                ).fetchall()
        return [dict(r) for r in rows]

    def get_by_alert(self, alert_id: int) -> dict[str, Any] | None:
        with sqlite3.connect(self._path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM alert_outcomes WHERE alert_id = ?",
                (alert_id,),
            ).fetchone()
        return dict(row) if row else None
