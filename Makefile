TERRAFORM_SANDBOX_DIR ?= ../datamailer-infra/datamailer-sandbox
LOCALSTACK_WAIT_SECONDS ?= 60

.PHONY: setup migrate run localstack localstack-up localstack-down test test-aws-local lint validate-infra smoke-sandbox smoke-sandbox-ses-events format

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

localstack-up:
	docker compose --profile aws-local up -d localstack
	@deadline=$$(($$(date +%s) + $(LOCALSTACK_WAIT_SECONDS))); \
	until curl -fsS http://localhost:4566/_localstack/health >/dev/null; do \
		if [ $$(date +%s) -ge $$deadline ]; then \
			echo "LocalStack did not become healthy within $(LOCALSTACK_WAIT_SECONDS)s"; \
			docker compose logs --tail=80 localstack; \
			exit 1; \
		fi; \
		echo "Waiting for LocalStack at http://localhost:4566"; \
		sleep 2; \
	done

localstack-down:
	docker compose --profile aws-local down

test:
	uv run pytest

test-aws-local: localstack-up
	uv run pytest tests_integration -m aws_local

lint:
	uv run ruff check .
	uv run python scripts/validate_infra.py

validate-infra:
	uv run python scripts/validate_infra.py

smoke-sandbox:
	uv run python scripts/smoke_test_sandbox.py --terraform-dir $(TERRAFORM_SANDBOX_DIR)

smoke-sandbox-ses-events:
	uv run python scripts/smoke_test_sandbox.py --terraform-dir $(TERRAFORM_SANDBOX_DIR) --ses-event-smoke

format:
	uv run ruff format .
