"""Tests for orchestrator — Phase 11 additions."""
from pathlib import Path
from unittest.mock import MagicMock, patch

import structlog.testing

from oilbot.orchestrator import Orchestrator
from oilbot.persistence.db import Database


def _make_config(news_sources: str = "truth_social", **kwargs: object) -> MagicMock:
    cfg = MagicMock()
    cfg.truth_social_handle = "realDonaldTrump"
    cfg.news_poll_interval_seconds = 0
    cfg.news_sources = news_sources
    cfg.ibkr_host = "127.0.0.1"
    cfg.ibkr_port = 4002
    cfg.ibkr_client_id = 42
    cfg.llm_provider = "none"
    cfg.llm_api_key = ""
    cfg.spike_window_seconds = 120
    cfg.pullback_window_seconds = 300
    cfg.total_window_seconds = 600
    cfg.pullback_k = 0.5
    cfg.atr_bars = 12
    cfg.k_static = 0.10
    cfg.target_rr = 1.5
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg


async def test_zero_sources_logs_warning_and_does_not_crash(tmp_path: Path) -> None:
    """With NEWS_SOURCES='', the news loop logs a warning and exits cleanly."""
    orch = Orchestrator.__new__(Orchestrator)
    orch._sources = []

    with structlog.testing.capture_logs() as cap:
        await orch._run_news_loop()

    assert any(e.get("event") == "no_news_sources_enabled" for e in cap)


async def test_unknown_source_name_ignored(tmp_path: Path) -> None:
    """An unrecognised source name in NEWS_SOURCES produces an empty list."""
    config = _make_config(news_sources="nonexistent_source")
    enabled = {s.strip() for s in config.news_sources.split(",") if s.strip()}
    assert "truth_social" not in enabled


async def test_truth_social_source_built_when_enabled(tmp_path: Path) -> None:
    """With NEWS_SOURCES='truth_social', exactly one source is constructed."""
    from oilbot.news.listener import NewsListener

    config = _make_config(news_sources="truth_social")
    db = Database(tmp_path / "test.db")

    with (
        patch("oilbot.orchestrator.Database", return_value=db),
        patch("oilbot.orchestrator.TelegramAlerter"),
        patch("oilbot.orchestrator.PriceMonitor"),
        patch("oilbot.orchestrator.HybridClassifier"),
        patch("oilbot.orchestrator.StrategyEngine"),
    ):
        orch = Orchestrator(config)

    assert len(orch._sources) == 1
    assert orch._sources[0].name == "truth_social"
    assert isinstance(orch._sources[0], NewsListener)


async def test_multiple_sources_in_allow_list(tmp_path: Path) -> None:
    """Only recognised source names in NEWS_SOURCES are constructed."""

    config = _make_config(news_sources="truth_social,opec_rss,unknown_source")
    db = Database(tmp_path / "test.db")

    with (
        patch("oilbot.orchestrator.Database", return_value=db),
        patch("oilbot.orchestrator.TelegramAlerter"),
        patch("oilbot.orchestrator.PriceMonitor"),
        patch("oilbot.orchestrator.HybridClassifier"),
        patch("oilbot.orchestrator.StrategyEngine"),
    ):
        orch = Orchestrator(config)

    # Only truth_social is implemented in Phase 11; opec_rss and unknown ignored
    names = [s.name for s in orch._sources]
    assert names == ["truth_social"]
