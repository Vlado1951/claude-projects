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
            ao.exit_price,
            ao.tracked_from_at,
            a.side,
            a.entry,
            a.stop,
            a.target,
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


def write_html(
    path: Path,
    rows: list[dict[str, Any]],
    overall: dict[str, Any],
    since: str | None,
    generated_at: datetime,
) -> None:
    """Write a self-contained HTML performance report."""
    import html as _html
    import json

    period = f"since {since}" if since else "all time"

    # ── stat card helper ─────────────────────────────────────────────────────
    def _card(label: str, value: str, css: str = "") -> str:
        return (
            f'<div class="stat"><div class="stat-label">{label}</div>'
            f'<div class="stat-value {css}">{value}</div></div>'
        )

    def _r_css(v: float | None) -> str:
        if v is None:
            return ""
        return "positive" if v >= 0 else "negative"

    # ── summary cards ────────────────────────────────────────────────────────
    if overall["total"] >= 5:
        cards = "".join([
            _card("Closed alerts", str(overall["total"])),
            _card("Hit rate", f"{overall['hit_rate']:.1%}", "positive"),
            _card("Stop rate", f"{overall['stop_rate']:.1%}", "negative"),
            _card("Timeout", f"{overall['timeout_rate']:.1%}"),
            _card("Avg R", _r(overall["avg_r"]), _r_css(overall["avg_r"])),
            _card("Median R", _r(overall["median_r"]), _r_css(overall["median_r"])),
            _card("Profit factor", _pf(overall["profit_factor"])),
            _card("Best trade", _r(overall["best_r"]), "positive"),
            _card("Worst trade", _r(overall["worst_r"]), "negative"),
        ])
    elif overall["total"] > 0:
        cards = _card(
            "Closed alerts",
            f"{overall['total']} — insufficient sample size",
        )
    else:
        cards = "<p>No closed alerts yet.</p>"

    # ── breakdown section helper ─────────────────────────────────────────────
    def _breakdown_section(title: str, dimension: str) -> str:
        if not rows:
            return ""
        groups = group_by(rows, dimension)
        items = []
        for key in sorted(groups):
            s = compute_stats(groups[key])
            if s["total"] == 0:
                continue
            if s["sufficient"]:
                detail = (
                    f"<small>Hit {s['hit_rate']:.0%} | "
                    f"Stop {s['stop_rate']:.0%} | "
                    f"Avg R {_r(s['avg_r'])} | "
                    f"PF {_pf(s['profit_factor'])}</small>"
                )
            else:
                detail = f"<small>{s['total']} alert(s) — insufficient sample</small>"
            items.append(
                f'<div class="breakdown-item">'
                f'<div class="breakdown-key">{_html.escape(str(key))}</div>'
                f"{detail}"
                f"</div>"
            )
        if not items:
            return ""
        return (
            f"<section><h2>{title}</h2>"
            f'<div class="breakdown-grid">{"".join(items)}</div></section>'
        )

    # ── chart data ───────────────────────────────────────────────────────────
    chart_labels = [str(r["alert_id"]) for r in rows]
    chart_data = [r["realized_r"] if r["realized_r"] is not None else 0 for r in rows]
    chart_colors = [
        "rgba(22,163,74,0.8)" if v >= 0 else "rgba(220,38,38,0.8)"
        for v in chart_data
    ]

    # ── trades table rows ────────────────────────────────────────────────────
    def _td(val: Any, fmt: str = "") -> str:
        if val is None:
            return '<td data-val="">—</td>'
        if isinstance(val, float):
            display = f"{val:.2f}"
            return f'<td data-val="{val}">{display}</td>'
        return f'<td data-val="{_html.escape(str(val))}">{_html.escape(str(val))}</td>'

    def _badge(status: str) -> str:
        return f'<span class="badge badge-{status}">{status}</span>'

    table_rows = []
    for r in rows:
        fired = (r.get("sent_at") or "")[:19].replace("T", " ")
        table_rows.append(
            f"<tr>"
            f"<td>{r['alert_id']}</td>"
            f"<td>{fired}</td>"
            f"<td>{_html.escape(r.get('source', ''))}</td>"
            f"<td>{r.get('side', '')}</td>"
            + _td(r.get("entry"))
            + _td(r.get("stop"))
            + _td(r.get("target"))
            + _td(r.get("exit_price"))
            + f"<td>{_badge(r['status'])}</td>"
            + _td(r.get("realized_r"))
            + _td(r.get("mfe_r"))
            + _td(r.get("mae_r"))
            + "</tr>"
        )

    # ── assemble HTML ────────────────────────────────────────────────────────
    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Oilbot Performance Report</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:system-ui,sans-serif;background:#f5f5f5;color:#222}}
h1{{background:#1a1a2e;color:#eee;padding:1rem 2rem}}
.meta{{color:#888;font-size:.85rem;padding:.5rem 2rem;background:#fff;border-bottom:1px solid #eee}}
section{{margin:1.5rem 2rem;background:#fff;border-radius:8px;padding:1.5rem;
box-shadow:0 1px 4px rgba(0,0,0,.1)}}
h2{{font-size:1.1rem;margin-bottom:1rem;color:#1a1a2e}}
.stat-grid{{display:flex;flex-wrap:wrap;gap:1rem}}
.stat{{flex:1 1 130px;background:#f0f4ff;border-radius:6px;padding:.75rem 1rem}}
.stat-label{{font-size:.7rem;color:#666;text-transform:uppercase;letter-spacing:.05em}}
.stat-value{{font-size:1.3rem;font-weight:bold;margin-top:.2rem}}
.positive{{color:#16a34a}}.negative{{color:#dc2626}}
.breakdown-grid{{display:flex;flex-wrap:wrap;gap:1rem}}
.breakdown-item{{flex:1 1 200px;background:#f8faff;border-radius:6px;padding:1rem}}
.breakdown-key{{font-weight:600;margin-bottom:.4rem}}
table{{width:100%;border-collapse:collapse;font-size:.82rem}}
th,td{{padding:.45rem .7rem;text-align:left;border-bottom:1px solid #eee;white-space:nowrap}}
th{{background:#f8f8f8;cursor:pointer;user-select:none;position:sticky;top:0}}
th:hover{{background:#ebebeb}}
tr:hover td{{background:#f0f7ff}}
.badge{{display:inline-block;border-radius:4px;padding:1px 7px;font-size:.75rem;font-weight:600}}
.badge-target_hit{{background:#dcfce7;color:#166534}}
.badge-stop_hit{{background:#fee2e2;color:#991b1b}}
.badge-timeout{{background:#fef3c7;color:#92400e}}
.chart-wrap{{max-height:300px}}
@media(max-width:600px){{section{{margin:.75rem;padding:1rem}}}}
</style>
</head>
<body>
<h1>Oilbot Performance Report</h1>
<div class="meta">
  Generated: {generated_at.strftime("%Y-%m-%d %H:%M UTC")} &nbsp;|&nbsp; Period: {period}
</div>

<section>
  <h2>Summary</h2>
  <div class="stat-grid">{cards}</div>
</section>

<section>
  <h2>R-Multiple per Trade</h2>
  <div class="chart-wrap">
    <canvas id="rChart"></canvas>
  </div>
</section>

{_breakdown_section("Breakdown by Source", "source")}
{_breakdown_section("Breakdown by Sentiment", "sentiment")}
{_breakdown_section("Breakdown by Confirmation", "confirmation")}

<section>
  <h2>All Trades</h2>
  <div style="overflow-x:auto">
  <table id="tradesTable">
    <thead><tr>
      <th>ID</th><th>Fired at</th><th>Source</th><th>Side</th>
      <th>Entry</th><th>Stop</th><th>Target</th><th>Exit</th>
      <th>Status</th><th>R</th><th>MFE R</th><th>MAE R</th>
    </tr></thead>
    <tbody>{"".join(table_rows)}</tbody>
  </table>
  </div>
</section>

<script>
(function(){{
  var ctx = document.getElementById('rChart');
  new Chart(ctx, {{
    type: 'bar',
    data: {{
      labels: {json.dumps(chart_labels)},
      datasets: [{{
        label: 'Realized R',
        data: {json.dumps(chart_data)},
        backgroundColor: {json.dumps(chart_colors)},
        borderRadius: 3
      }}]
    }},
    options: {{
      responsive: true,
      plugins: {{ legend: {{ display: false }} }},
      scales: {{
        y: {{ title: {{ display: true, text: 'R-multiple' }}, grid: {{ color: '#eee' }} }},
        x: {{ title: {{ display: true, text: 'Alert ID' }} }}
      }}
    }}
  }});

  var table = document.getElementById('tradesTable');
  var sortCol = -1, sortAsc = true;
  table.querySelectorAll('th').forEach(function(th, i) {{
    th.addEventListener('click', function() {{
      if (sortCol === i) sortAsc = !sortAsc; else {{ sortCol = i; sortAsc = true; }}
      var tbody = table.querySelector('tbody');
      var rows = Array.from(tbody.querySelectorAll('tr'));
      rows.sort(function(a, b) {{
        var av = a.cells[i].dataset.val || a.cells[i].textContent;
        var bv = b.cells[i].dataset.val || b.cells[i].textContent;
        var an = parseFloat(av), bn = parseFloat(bv);
        if (!isNaN(an) && !isNaN(bn)) return sortAsc ? an - bn : bn - an;
        return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
      }});
      rows.forEach(function(r) {{ tbody.appendChild(r); }});
    }});
  }});
}})();
</script>
</body>
</html>"""

    path.write_text(doc, encoding="utf-8")


async def performance(
    config: Config,
    since: str | None = None,
    by: str | None = None,
    html: str | None = None,
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
    rows = fetch_rows(DEFAULT_DB_PATH, since_dt)
    closed = len(rows)
    overall = compute_stats(rows)

    if total_fired == 0:
        print("No alerts recorded in this period.")
    else:
        print(f"Total alerts fired: {total_fired}  |  Closed: {closed}  |  Tracking: {tracking}")
        print()
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

    if html:
        html_path = Path(html)
        write_html(html_path, rows, overall, since, generated_at=datetime.now(UTC))
        print(f"HTML report written to: {html_path.resolve()}")
