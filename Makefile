PYTHON ?= python3

.PHONY: install dev lint test run

install:
	uv pip install -e ".[dev]"

dev:
	uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

lint:
	uv run ruff check .

test:
	uv run pytest --cov=app --cov-report=term-missing

run:
	uv run uvicorn app.main:app --host 0.0.0.0 --port 8000

