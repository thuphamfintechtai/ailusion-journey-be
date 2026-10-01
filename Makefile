.DEFAULT_GOAL := help
COMPOSE := docker compose
# Cổng cho `make dev` (chạy thẳng trên máy). Stack Docker dùng API_HOST_PORT trong .env.
PORT ?= 8000

help: ## Liệt kê lệnh
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

env: ## Tạo .env từ .env.example (không ghi đè)
	@test -f .env && echo ".env đã tồn tại" || (cp .env.example .env && echo "Đã tạo .env — chỉnh lại nếu cần")

install: ## Cài dependency (uv sync) + pre-commit hook nếu có config
	uv sync
	@test -f .pre-commit-config.yaml \
		&& uv run pre-commit install \
		|| echo "Bỏ qua pre-commit: chưa có .pre-commit-config.yaml"

# ---------------------------------------------------------------- Dev trên máy
services: ## Bật PostgreSQL + Redis bằng Docker (không chạy API)
	$(COMPOSE) up -d --wait db redis

migrate: ## alembic upgrade head
	uv run alembic upgrade head

revision: ## Sinh migration, vd: make revision m="create trips table"
	@test -n "$(m)" || { echo 'Thiếu tên: make revision m="create trips table"'; exit 1; }
	uv run alembic revision --autogenerate -m "$(m)"

dev: ## Chạy API có reload, mặc định :8000 — đổi bằng `make dev PORT=8080`
	uv run uvicorn app.main:app --reload --port $(PORT)

superuser: ## Tạo/nâng quyền superuser, vd: make superuser email=admin@example.com
	@test -n "$(email)" || { echo 'Thiếu email: make superuser email=admin@example.com'; exit 1; }
	uv run python -m app.scripts.create_superuser --email $(email)

# ---------------------------------------------------------------- Stack Docker
up: ## Bật cả stack: db + redis + migrate + api
	$(COMPOSE) up -d --build --wait

down: ## Tắt container (giữ dữ liệu)
	$(COMPOSE) down

ps: ## Trạng thái container
	$(COMPOSE) ps

logs: ## Xem log
	$(COMPOSE) logs -f --tail=100

# ---------------------------------------------------------------- Kiểm tra code
lint: ## Ruff
	uv run ruff check app tests

typecheck: ## mypy (strict)
	uv run mypy app

test: ## pytest (SQLite in-memory + fakeredis, không cần Docker)
	uv run pytest

check: lint typecheck test ## Chạy cả lint + typecheck + test

reset-data: ## XOÁ toàn bộ dữ liệu PostgreSQL + Redis (hỏi lại trước)
	@read -p "Xoá volume pgdata, redisdata? [y/N] " a; [ "$$a" = "y" ] && $(COMPOSE) down -v || echo "Huỷ"

.PHONY: help env install services migrate revision dev superuser up down ps logs lint typecheck test check reset-data
