.PHONY: setup migrate run test lint format

setup:
	@test -f .env || cp .env.example .env
	uv sync
	uv run python manage.py migrate

migrate:
	uv run python manage.py migrate

run:
	uv run python manage.py runserver

test:
	uv run pytest

lint:
	uv run ruff check .

format:
	uv run ruff format .
