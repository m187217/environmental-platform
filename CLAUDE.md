# Environmental Platform — 中国企业环境评估报告共享与匹配平台

## Architecture
- Frontend: Vue3 + TypeScript (苹果官网风格)
- Backend: FastAPI (Python 3.11) + Node.js Express API Gateway
- Storage: PostgreSQL + MinIO + Elasticsearch 8.x + Redis
- Deployment: Docker Compose (dev) → Kubernetes (prod)

## Project Structure
```
src/
├── crawler/      # 爬虫Agent — Scrapy + Playwright
├── processor/    # 文档处理Agent — pdfplumber + BERT
├── security/     # 安全Agent — FastAPI middleware
├── user/         # 用户系统 — JWT + OAuth2
├── api/          # API Gateway — FastAPI routes
└── models/       # 数据模型 — Pydantic schemas
```

## Key Commands
- `make test` — run all tests
- `make lint` — ruff + mypy
- `dodcker compose up` — dev environment

## Code Standards
- Python 3.11+, type hints on all functions
- 4-space indent, Google-style docstrings
- Test file: `test_<module>.py` in `tests/`
- FastAPI for all HTTP endpoints
- Pydantic v2 for data validation
- Error handling: structured JSON errors with {code, message, detail}
- Logging: structlog for JSON-formatted logs
