.PHONY: setup migrate run localstack test test-aws-local lint validate-infra smoke-sandbox smoke-sandbox-ses-events format

setup:
	@test -f .env || cp .env.example .env
	uv sync
	uv run python manage.py migrate

migrate:
	uv run python manage.py migrate

run:
	uv run python manage.py runserver

localstack:
	docker compose --profile aws-local up localstack

test:
	uv run pytest

test-aws-local:
	uv run pytest -m aws_local

lint:
	uv run ruff check .
	uv run python scripts/validate_infra.py

validate-infra:
	uv run python scripts/validate_infra.py

smoke-sandbox:
	uv run python scripts/smoke_test_sandbox.py --terraform-dir terraform/datamailer-sandbox

smoke-sandbox-ses-events:
	uv run python scripts/smoke_test_sandbox.py --terraform-dir terraform/datamailer-sandbox --ses-event-smoke

format:
	uv run ruff format .
