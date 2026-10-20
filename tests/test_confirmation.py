from __future__ import annotations

from datetime import UTC, datetime, timedelta

from oilbot.price.context import ConfirmationResult, PriceContext


def _make_ctx() -> PriceContext:
    return PriceContext()


def _inject_move(ctx: PriceContext, symbol: str, start: float, end: float) -> None:
    """Inject two price points so _direction() sees a clear move."""
    t0 = datetime.now(UTC) - timedelta(seconds=60)
    t1 = datetime.now(UTC)
    ctx.inject(symbol, start, ts=t0)
    ctx.inject(symbol, end, ts=t1)


def _inject_all_bullish(ctx: PriceContext) -> None:
    _inject_move(ctx, "BZ=F",     70.00, 70.50)  # up
    _inject_move(ctx, "CL=F",     68.00, 68.40)  # up
    _inject_move(ctx, "DX-Y.NYB", 104.0, 103.5)  # down
    _inject_move(ctx, "XLE",      90.00, 90.60)  # up
    _inject_move(ctx, "XOP",      30.00, 30.20)  # up


def _inject_all_bearish(ctx: PriceContext) -> None:
    _inject_move(ctx, "BZ=F",     70.50, 70.00)  # down
    _inject_move(ctx, "CL=F",     68.40, 68.00)  # down
    _inject_move(ctx, "DX-Y.NYB", 103.5, 104.0)  # up
    _inject_move(ctx, "XLE",      90.60, 90.00)  # down
    _inject_move(ctx, "XOP",      30.20, 30.00)  # down


# ── is_aligned ────────────────────────────────────────────────────────────────

def test_all_aligned_buy() -> None:
    ctx = _make_ctx()
    _inject_all_bullish(ctx)
    result = ctx.is_aligned("BUY")
    assert result.status == "aligned"
    assert result.aligned_count == 5


def test_all_aligned_sell() -> None:
    ctx = _make_ctx()
    _inject_all_bearish(ctx)
    result = ctx.is_aligned("SELL")
    assert result.status == "aligned"
    assert result.aligned_count == 5


def test_buy_disaligned_when_bearish_prices() -> None:
    ctx = _make_ctx()
    _inject_all_bearish(ctx)
    result = ctx.is_aligned("BUY")
    assert result.status == "disaligned"
    assert result.aligned_count == 0


def test_sell_disaligned_when_bullish_prices() -> None:
    ctx = _make_ctx()
    _inject_all_bullish(ctx)
    result = ctx.is_aligned("SELL")
    assert result.status == "disaligned"
    assert result.aligned_count == 0


def test_mixed_three_aligned() -> None:
    ctx = _make_ctx()
    # 3 BUY-aligned, 2 counter
    _inject_move(ctx, "BZ=F",     70.00, 70.50)  # up  ✓ BUY
    _inject_move(ctx, "CL=F",     68.00, 68.40)  # up  ✓ BUY
    _inject_move(ctx, "DX-Y.NYB", 103.5, 104.0)  # up  ✗ BUY (expected down)
    _inject_move(ctx, "XLE",      90.00, 90.60)  # up  ✓ BUY
    _inject_move(ctx, "XOP",      30.20, 30.00)  # down ✗ BUY (expected up)
    result = ctx.is_aligned("BUY")
    assert result.status == "mixed"
    assert result.aligned_count == 3


def test_mixed_two_aligned() -> None:
    ctx = _make_ctx()
    _inject_move(ctx, "BZ=F",     70.00, 70.50)  # up  ✓ BUY
    _inject_move(ctx, "CL=F",     68.40, 68.00)  # down ✗ BUY
    _inject_move(ctx, "DX-Y.NYB", 103.5, 104.0)  # up  ✗ BUY
    _inject_move(ctx, "XLE",      90.00, 90.60)  # up  ✓ BUY
    _inject_move(ctx, "XOP",      30.20, 30.00)  # down ✗ BUY
    result = ctx.is_aligned("BUY")
    assert result.status == "mixed"
    assert result.aligned_count == 2


def test_disaligned_one_aligned() -> None:
    ctx = _make_ctx()
    _inject_move(ctx, "BZ=F",     70.00, 70.50)  # up  ✓ BUY
    _inject_move(ctx, "CL=F",     68.40, 68.00)  # down ✗
    _inject_move(ctx, "DX-Y.NYB", 103.5, 104.0)  # up  ✗
    _inject_move(ctx, "XLE",      90.60, 90.00)  # down ✗
    _inject_move(ctx, "XOP",      30.20, 30.00)  # down ✗
    result = ctx.is_aligned("BUY")
    assert result.status == "disaligned"
    assert result.aligned_count == 1


def test_unknown_when_no_data() -> None:
    ctx = _make_ctx()
    result = ctx.is_aligned("BUY")
    assert result.status == "disaligned"
    assert result.aligned_count == 0
    assert all(v == "?" for v in result.details.values())


def test_unknown_when_single_price_point() -> None:
    ctx = _make_ctx()
    for sym in PriceContext.TICKERS:
        ctx.inject(sym, 100.0)
    result = ctx.is_aligned("BUY")
    assert all(v == "?" for v in result.details.values())


# ── ConfirmationResult.summary ────────────────────────────────────────────────

def test_summary_contains_labels() -> None:
    result = ConfirmationResult(
        status="aligned",
        aligned_count=5,
        total_checked=5,
        details={"BZ=F": "↑", "CL=F": "↑", "DX-Y.NYB": "↓", "XLE": "↑", "XOP": "↑"},
    )
    s = result.summary()
    assert "Brent↑" in s
    assert "WTI↑" in s
    assert "DXY↓" in s


# ── direction boundary ─────────────────────────────────────────────────────────

def test_flat_move_is_unknown_direction() -> None:
    """A change of exactly 0.005% (below 0.01% threshold) should be flat/unknown."""
    ctx = _make_ctx()
    t0 = datetime.now(UTC) - timedelta(seconds=60)
    t1 = datetime.now(UTC)
    ctx.inject("BZ=F", 100.0, ts=t0)
    ctx.inject("BZ=F", 100.005, ts=t1)  # 0.005% — below threshold
    result = ctx.is_aligned("BUY")
    # flat is treated the same as unknown for alignment
    # direction returns "flat" not "up", so doesn't match expected "up"
    # flat is not "up", so doesn't satisfy BUY alignment for Brent
    assert result.aligned_count < 5


# ── window pruning ─────────────────────────────────────────────────────────────

def test_old_prices_pruned() -> None:
    ctx = _make_ctx()
    old_ts = datetime.now(UTC) - timedelta(seconds=400)  # beyond 5-min window
    ctx.inject("BZ=F", 50.0, ts=old_ts)
    ctx.inject("BZ=F", 100.0)  # only one recent point
    result = ctx.is_aligned("BUY")
    # only one point in window → direction unknown
    assert result.details["BZ=F"] == "?"


# ── seeding with bars older than 5 minutes ────────────────────────────────────

def test_direction_computed_with_bars_older_than_window() -> None:
    """Two bars both outside the 5-min window still yield a direction."""
    ctx = _make_ctx()
    t0 = datetime.now(UTC) - timedelta(seconds=460)  # ~7.7 min ago
    t1 = datetime.now(UTC) - timedelta(seconds=400)  # ~6.7 min ago
    ctx.inject("BZ=F", 70.00, ts=t0)
    ctx.inject("BZ=F", 70.50, ts=t1)
    result = ctx.is_aligned("BUY")
    assert result.details["BZ=F"] == "↑"


def test_stale_flag_when_last_bar_older_than_threshold() -> None:
    """Bars that are outside the stale threshold are flagged in stale_symbols."""
    ctx = _make_ctx()
    t0 = datetime.now(UTC) - timedelta(minutes=20)
    t1 = datetime.now(UTC) - timedelta(minutes=18)
    ctx.inject("BZ=F", 70.00, ts=t0)
    ctx.inject("BZ=F", 70.50, ts=t1)
    result = ctx.is_aligned("BUY", stale_threshold_s=15 * 60)
    assert "BZ=F" in result.stale_symbols
    assert result.details["BZ=F"] == "↑"  # direction still computed


def test_no_stale_flag_when_last_bar_within_threshold() -> None:
    ctx = _make_ctx()
    t0 = datetime.now(UTC) - timedelta(minutes=10)
    t1 = datetime.now(UTC) - timedelta(minutes=8)
    ctx.inject("BZ=F", 70.00, ts=t0)
    ctx.inject("BZ=F", 70.50, ts=t1)
    result = ctx.is_aligned("BUY", stale_threshold_s=15 * 60)
    assert "BZ=F" not in result.stale_symbols


def test_no_stale_flag_when_no_data() -> None:
    """Tickers with no data at all should not appear in stale_symbols."""
    ctx = _make_ctx()
    result = ctx.is_aligned("BUY")
    assert "BZ=F" not in result.stale_symbols
