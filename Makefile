.PHONY: lint test run install test-eia test-rss test-calendar test-confirmation

install:
	uv sync --extra dev

lint:
	uv run ruff check .
	uv run mypy oilbot

test:
	uv run pytest

run:
	uv run python -m oilbot run

test-eia:
	uv run python -m oilbot test-eia

test-rss:
	uv run python -m oilbot test-rss

test-calendar:
	uv run python -m oilbot test-calendar

test-confirmation:
	uv run python -m oilbot test-confirmation
