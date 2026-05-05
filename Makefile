.PHONY: test lint dev build deploy clean

test:
	pytest tests/ -v --cov=src --cov-report=term-missing

lint:
	ruff check src/ tests/
	mypy src/ --ignore-missing-imports

dev:
	uvicorn src.main:app --reload --host 0.0.0.0 --port 8000

build:
	docker compose -f docker/docker-compose.yml build

deploy:
	docker compose -f docker/docker-compose.yml up -d

down:
	docker compose -f docker/docker-compose.yml down

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
