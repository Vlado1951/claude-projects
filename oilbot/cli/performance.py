from __future__ import annotations

import sqlite3
import statistics
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from oilbot.config import Config
from oilbot.persistence.db import DEFAULT_DB_PATH

_BY_CHOICES = ("source", "sentiment", "confirmation", "hour")


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------


def parse_since(s: str) -> datetime:
    """Parse '7d', '24h', '2w' → UTC datetime cutoff."""
    unit = s[-1].lower()
    try:
        value = int(s[:-1])
    except ValueError:
        raise ValueError(f"Invalid duration {s!r}. Expected e.g. 7d, 24h, 2w.") from None
    if unit == "h":
        return datetime.now(UTC) - timedelta(hours=value)
    if unit == "d":
        return datetime.now(UTC) - timedelta(days=value)
    if unit == "w":
        return datetime.now(UTC) - timedelta(weeks=value)
    raise ValueError(f"Unknown unit {unit!r} in {s!r}. Use h, d, or w.")


# ---------------------------------------------------------------------------
# DB query
# ---------------------------------------------------------------------------


def fetch_rows(db_path: Path, since: datetime | None = None) -> list[dict[str, Any]]:
    """Return all closed outcome rows joined with alert, source, sentiment fields."""
    query = """
        SELECT
            ao.alert_id,
            ao.status,
            ao.realized_r,
            ao.mfe_r,
            ao.mae_r,
            ao.tracked_from_at,
            a.side,
            a.sent_at,
            a.confirmation_status,
            COALESCE(sp.source, 'unknown')    AS source,
            COALESCE(c.sentiment, 'UNKNOWN')  AS sentiment
        FROM alert_outcomes ao
        JOIN alerts a ON a.id = ao.alert_id
        LEFT JOIN (
            SELECT id, MIN(source) AS source FROM seen_posts GROUP BY id
        ) sp ON sp.id = a.post_id
        LEFT JOIN classifications c ON c.post_id = a.post_id
        WHERE ao.status != 'tracking'
    """
    params: list[str] = []
    if since:
        query += " AND ao.tracked_from_at >= ?"
        params.append(since.isoformat())
    query += " ORDER BY ao.alert_id"

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def fetch_total_and_tracking(db_path: Path, since: datetime | None = None) -> tuple[int, int]:
    """Return (total_fired, currently_tracking) counts."""
    since_clause = "AND ao.tracked_from_at >= ?" if since else ""
    params: list[str] = [since.isoformat()] if since else []
    with sqlite3.connect(db_path) as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM alert_outcomes ao WHERE 1=1 {since_clause}", params
        ).fetchone()[0]
        tracking = conn.execute(
            f"SELECT COUNT(*) FROM alert_outcomes ao WHERE status='tracking' {since_clause}",
            params,
        ).fetchone()[0]
    return total, tracking


# ---------------------------------------------------------------------------
# Stats aggregation
# ---------------------------------------------------------------------------


def compute_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute performance statistics from a list of closed outcome rows."""
    total = len(rows)
    if total == 0:
        return {"total": 0, "sufficient": False}

    target_hit = sum(1 for r in rows if r["status"] == "target_hit")
    stop_hit = sum(1 for r in rows if r["status"] == "stop_hit")
    timeout = sum(1 for r in rows if r["status"] == "timeout")

    rs = [r["realized_r"] for r in rows if r["realized_r"] is not None]
    mfe_rs = [r["mfe_r"] for r in rows if r["mfe_r"] is not None]
    mae_rs = [r["mae_r"] for r in rows if r["mae_r"] is not None]

    winners = [r for r in rs if r > 0]
    losers = [r for r in rs if r < 0]
    if winners and losers:
        profit_factor: float | None = sum(winners) / abs(sum(losers))
    elif winners:
        profit_factor = float("inf")
    else:
        profit_factor = 0.0 if rs else None

    return {
        "total": total,
        "sufficient": total >= 5,
        "target_hit": target_hit,
        "stop_hit": stop_hit,
        "timeout": timeout,
        "hit_rate": target_hit / total,
        "stop_rate": stop_hit / total,
        "timeout_rate": timeout / total,
        "avg_r": statistics.mean(rs) if rs else None,
        "median_r": statistics.median(rs) if rs else None,
        "profit_factor": profit_factor,
        "avg_mfe_r": statistics.mean(mfe_rs) if mfe_rs else None,
        "avg_mae_r": statistics.mean(mae_rs) if mae_rs else None,
        "best_r": max(rs) if rs else None,
        "worst_r": min(rs) if rs else None,
    }


# ---------------------------------------------------------------------------
# Text formatting
# ---------------------------------------------------------------------------


def _r(v: float | None) -> str:
    return f"{v:+.2f}R" if v is not None else "n/a"


def _pct(v: float) -> str:
    return f"{v:.1%}"


def _pf(v: float | None) -> str:
    if v is None:
        return "n/a"
    if v == float("inf"):
        return "∞"
    return f"{v:.2f}"


def format_stats_block(stats: dict[str, Any], indent: str = "") -> str:
    """Format a stats dict as a multi-line text block."""
    lines: list[str] = []
    total = stats["total"]

    if total == 0:
        lines.append(f"{indent}No closed alerts in this period.")
        return "\n".join(lines)

    if not stats["sufficient"]:
        lines.append(
            f"{indent}{total} closed alert(s) — insufficient sample size, more data needed."
        )
        return "\n".join(lines)

    lines.append(
        f"{indent}Closed: {total}"
        f"  |  Target hit: {stats['target_hit']} ({_pct(stats['hit_rate'])})"
        f"  |  Stop: {stats['stop_hit']} ({_pct(stats['stop_rate'])})"
        f"  |  Timeout: {stats['timeout']} ({_pct(stats['timeout_rate'])})"
    )
    lines.append(
        f"{indent}Avg R: {_r(stats['avg_r'])}"
        f"  |  Median R: {_r(stats['median_r'])}"
        f"  |  Profit factor: {_pf(stats['profit_factor'])}"
    )
    lines.append(
        f"{indent}Avg MFE: {_r(stats['avg_mfe_r'])}"
        f"  |  Avg MAE: {_r(stats['avg_mae_r'])}"
        f"  |  Best: {_r(stats['best_r'])}"
        f"  |  Worst: {_r(stats['worst_r'])}"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Breakdown grouping
# ---------------------------------------------------------------------------


def group_by(rows: list[dict[str, Any]], dimension: str) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if dimension == "source":
            key = row["source"]
        elif dimension == "sentiment":
            key = row["sentiment"]
        elif dimension == "confirmation":
            key = row["confirmation_status"]
        elif dimension == "hour":
            try:
                dt = datetime.fromisoformat(row["sent_at"])
                key = f"{dt.hour:02d}:00 UTC"
            except (ValueError, TypeError):
                key = "unknown"
        else:
            key = "all"
        groups.setdefault(key, []).append(row)
    return groups


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


async def performance(
    config: Config,
    since: str | None = None,
    by: str | None = None,
) -> None:
    since_dt: datetime | None = None
    if since:
        try:
            since_dt = parse_since(since)
        except ValueError as exc:
            print(f"Error: {exc}")
            return

    period_label = f"since {since}" if since else "all time"
    header = f"Performance — {period_label}"
    print(header)
    print("─" * len(header))

    total_fired, tracking = fetch_total_and_tracking(DEFAULT_DB_PATH, since_dt)
    if total_fired == 0:
        print("No alerts recorded in this period.")
        return

    rows = fetch_rows(DEFAULT_DB_PATH, since_dt)
    closed = len(rows)

    print(f"Total alerts fired: {total_fired}  |  Closed: {closed}  |  Tracking: {tracking}")
    print()

    overall = compute_stats(rows)
    print(format_stats_block(overall))

    if by and rows:
        print()
        print(f"Breakdown by {by}:")
        print("─" * 40)
        groups = group_by(rows, by)
        for key in sorted(groups):
            print(f"  [{key}]")
            print(format_stats_block(compute_stats(groups[key]), indent="    "))
            print()
