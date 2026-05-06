# ═══════════════════════════════════════════════════════════════════════════════
# Environmental Platform — Makefile
# ═══════════════════════════════════════════════════════════════════════════════

.PHONY: help dev test lint build deploy down logs ps restart clean

# ═══ Default ═══

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ═══ Development ═══

dev: ## Start API in development mode (hot reload)
	PYTHONPATH=. uvicorn src.main:app --reload --host 0.0.0.0 --port 8000

test: ## Run tests
	pytest tests/ -v --cov=src --cov-report=term-missing

lint: ## Run linters
	ruff check src/ tests/ 2>/dev/null || echo "ruff not installed"
	mypy src/ --ignore-missing-imports 2>/dev/null || echo "mypy not installed"

# ═══ Docker ═══

build: ## Build Docker images
	docker compose -f docker/docker-compose.yml build

deploy: ## Start all services (PostgreSQL + Redis + ES + MinIO + API + Nginx)
	docker compose -f docker/docker-compose.yml up -d
	@echo ""
	@echo "🟢 Services starting..."
	@echo "   API:       http://localhost:80"
	@echo "   Swagger:   http://localhost:80/docs"
	@echo "   MinIO:     http://localhost:9001 (minioadmin/minioadmin)"
	@echo "   PG Admin:  localhost:5432 (postgres/postgres)"
	@echo ""
	@echo "Waiting for services to be healthy..."
	@timeout 60 docker compose -f docker/docker-compose.yml ps || true

down: ## Stop all services
	docker compose -f docker/docker-compose.yml down

restart: ## Restart all services
	docker compose -f docker/docker-compose.yml restart

logs: ## Tail all service logs
	docker compose -f docker/docker-compose.yml logs -f --tail=100

ps: ## List running services
	docker compose -f docker/docker-compose.yml ps

# ═══ Individual services ═══

db-shell: ## PostgreSQL shell
	docker compose -f docker/docker-compose.yml exec postgres psql -U postgres -d environmental

redis-cli: ## Redis CLI
	docker compose -f docker/docker-compose.yml exec redis redis-cli

es-health: ## Elasticsearch health
	curl -s http://localhost:9200/_cluster/health | python3 -m json.tool

# ═══ Maintenance ═══

clean: ## Remove __pycache__, .pyc files
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
	find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true

clean-docker: ## Remove all Docker volumes (WARNING: deletes all data)
	docker compose -f docker/docker-compose.yml down -v

reset: clean-docker deploy ## Full reset: delete data + restart
