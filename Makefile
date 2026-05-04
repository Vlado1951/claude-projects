.PHONY: lint test run install

install:
	uv sync --extra dev

lint:
	uv run ruff check .
	uv run mypy oilbot

test:
	uv run pytest

run:
	uv run python -m oilbot run
