.PHONY: up down build lint format test test-unit test-integration logs ps clean

up:
	docker compose up -d --build

down:
	docker compose down -v

build:
	docker compose build

lint:
	flake8 src tests
	black --check src tests
	isort --check-only src tests

format:
	black src tests
	isort src tests

test-unit:
	pytest tests/unit -v -m unit

test-integration:
	pytest tests/integration -v -m integration

test:
	pytest tests -v

logs:
	docker compose logs -f

ps:
	docker compose ps

clean:
	docker compose down -v --remove-orphans
	find . -type d -name "__pycache__" -exec rm -rf {} +
