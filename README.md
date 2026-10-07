# ailusion-journey-be

Backend API cho Ailusion Journey — **FastAPI** + **PostgreSQL** (SQLAlchemy 2 async, Alembic) + **Redis**, quản lý package bằng **uv**.

## Yêu cầu

- [uv](https://docs.astral.sh/uv/) (tự tải Python 3.13 theo `.python-version`)
- Docker Desktop (để chạy PostgreSQL + Redis, hoặc cả stack)

## Chạy nhanh (dev)

```bash
make env                      # tạo .env từ .env.example rồi chỉnh nếu cần
make install                  # uv sync (+ pre-commit hook nếu có .pre-commit-config.yaml)
make services                 # bật PostgreSQL + Redis bằng Docker
make migrate                  # alembic upgrade head
make dev                      # http://localhost:8000/docs
```

`make help` liệt kê tất cả lệnh. Không có `make`? Dùng trực tiếp lệnh tương ứng, ví dụ
`uv run uvicorn app.main:app --reload`.

Tạo tài khoản admin:

```bash
make superuser email=admin@example.com      # sẽ hỏi password
```

## Chạy cả stack bằng Docker

```bash
cp .env.example .env
make up        # db + redis + migrate (chạy 1 lần) + api
make logs
make down
```

Trùng port 5432/6379/8000 trên máy? Đổi `DB_HOST_PORT`, `REDIS_HOST_PORT`, `API_HOST_PORT` trong `.env`.

## Cấu trúc thư mục

```
app/
├── main.py                 # create_app(): lifespan, middleware, routers
├── core/
│   ├── config.py           # Settings (pydantic-settings, đọc .env)
│   ├── security.py         # hash password (Argon2), tạo/giải mã JWT
│   ├── exceptions.py       # AppError + handler → {"detail", "code"}
│   ├── middleware.py       # X-Request-ID + access log
│   ├── redis.py            # Redis client + dependency
│   ├── llm.py              # HTTP client sang service LLM + dependency
│   └── logging.py
├── db/
│   ├── base.py             # DeclarativeBase, UUID PK + timestamp mixin
│   └── session.py          # async engine, get_db()
├── models/                 # SQLAlchemy models (import vào models/__init__.py)
├── schemas/                # Pydantic request/response
├── services/               # business logic (nhận AsyncSession, tự commit)
├── api/
│   ├── deps.py             # DbSession, RedisClient, LlmClient, CurrentUser, CurrentSuperuser
│   ├── health.py           # /health, /health/ready
│   └── v1/
│       ├── router.py
│       └── routes/         # auth.py, users.py, chat.py
└── scripts/create_superuser.py
alembic/                    # migrations (async)
tests/                      # pytest + httpx
```

## API có sẵn

| Method | Path | Mô tả |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/health/ready` | Kiểm tra PostgreSQL + Redis (503 nếu lỗi) |
| POST | `/api/v1/auth/register` | Đăng ký |
| POST | `/api/v1/auth/login` | Đăng nhập (JSON) → access + refresh token |
| POST | `/api/v1/auth/token` | Đăng nhập OAuth2 form (dùng cho nút **Authorize** trong Swagger) |
| POST | `/api/v1/auth/refresh` | Đổi refresh token lấy cặp token mới (token cũ bị thu hồi) |
| POST | `/api/v1/auth/logout` | Thu hồi refresh token |
| POST | `/api/v1/auth/forgot-password` | `{"email"}` → luôn 202, gửi email chứa link đặt lại mật khẩu nếu email tồn tại |
| POST | `/api/v1/auth/reset-password` | `{"token", "new_password"}` → 204; token sai/hết hạn/đã dùng → 400 `invalid_reset_token` |
| GET / PATCH | `/api/v1/users/me` | Xem / cập nhật thông tin của mình |
| GET | `/api/v1/users` | Danh sách user (superuser, phân trang `limit`/`offset`) |
| GET | `/api/v1/users/{id}` | Chi tiết user (superuser) |
| POST | `/api/v1/chat` | Hỏi agent du lịch, chờ câu trả lời đầy đủ |
| POST | `/api/v1/chat/stream` | Như trên nhưng trả lời dần (SSE) |
| GET | `/api/v1/chat/threads` | Hội thoại của mình, mới nhất trước |
| GET | `/api/v1/chat/threads/{id}` | Lịch sử một hội thoại |
| DELETE | `/api/v1/chat/threads/{id}` | Bỏ hội thoại khỏi danh sách |

**Về token:** access token sống ngắn (mặc định 30 phút) và không bị thu hồi khi logout; refresh token (7 ngày) được xoay vòng mỗi lần `/refresh`, và `jti` của token đã dùng/đã logout được lưu trong Redis tới khi hết hạn.

**Quên mật khẩu:** `forgot-password` trả cùng một response (202) dù email có tồn tại hay không, và gửi email trong background task nên thời gian phản hồi cũng không lộ. Link có dạng `{FRONTEND_URL}/reset-password?token=...`; token là JWT loại `reset` (không dùng thay access/refresh token được), sống `PASSWORD_RESET_TOKEN_EXPIRE_MINUTES` phút (mặc định 30), chỉ dùng được một lần (`jti` đánh dấu trong Redis) và tự mất hiệu lực khi mật khẩu đã đổi. Đặt lại thành công thì mọi refresh token cấp trước đó bị từ chối (access token cũ vẫn sống tới khi hết hạn). Email gửi qua SMTP (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_TLS`); để trống `SMTP_HOST` khi dev thì link được in ra log (INFO) thay vì gửi.

## Chat với agent du lịch

Phần trả lời do service LLM riêng đảm nhiệm (repo **ailusion-journey-llm**, LangGraph + Gemini).
BE không gọi thẳng Gemini — nó gọi HTTP sang service đó và lo phần người dùng: ai được đụng
hội thoại nào.

```
Frontend ──► BE :8000 ──► LLM service :8001 ──► Gemini
                │                  │
          ai sở hữu thread     lịch sử hội thoại
          (Redis của BE)       (Redis của LLM service)
```

Chạy hai service:

```bash
# cửa sổ 1 — repo ailusion-journey-llm
cd ../ailusion-journey-llm && make up && make api   # :8001

# cửa sổ 2 — repo này
make services && make migrate && make dev           # :8000
```

Trỏ BE sang service LLM bằng `LLM_SERVICE_URL` trong `.env` (mặc định `http://localhost:8001`).

```bash
TOKEN=...   # lấy từ /api/v1/auth/login

# Lượt đầu: không gửi thread_id, BE trả về mã hội thoại
curl -X POST localhost:8000/api/v1/chat -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Đi Đà Lạt 3 ngày, 2 người lớn 1 bé, ngân sách 15tr?"}'
# -> {"thread_id":"chat-d8e51327dad4","reply":"..."}

# Lượt sau: gửi lại thread_id, agent nhớ ngữ cảnh
curl -X POST localhost:8000/api/v1/chat -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"message":"Nhắc lại tôi đi mấy người?","thread_id":"chat-d8e51327dad4"}'
```

**Frontend không cần gửi lại lịch sử** — chỉ giữ `thread_id` rồi gửi kèm mỗi lượt.

Vài điểm đáng lưu ý:

- Mọi endpoint chat đều cần đăng nhập. BE giữ sổ "thread nào của ai" trong Redis
  (`chat:threads:<user_id>`, sorted set theo thời điểm dùng gần nhất). Dùng `thread_id` của
  người khác thì nhận **404** — cố ý không trả 403 để không lộ việc thread đó có tồn tại.
- Service LLM chết hoặc quá chậm → BE trả **503** `{"code": "upstream_unavailable"}`,
  không phải 500.
- Hội thoại hết hạn theo `CHAT_THREAD_TTL_MINUTES`; để khớp với `CHECKPOINT_TTL_MINUTES`
  bên service LLM, nếu không danh sách sẽ còn tên những hội thoại đã bị xoá ở đầu kia.

## Thêm một tính năng mới

1. Model: `app/models/<name>.py`, rồi import trong `app/models/__init__.py`
2. Migration: `make revision m="create <name> table"` → xem lại file trong `alembic/versions/` → `make migrate`
3. Schema: `app/schemas/<name>.py`
4. Service: `app/services/<name>_service.py`
5. Route: `app/api/v1/routes/<name>.py`, rồi `include_router` trong `app/api/v1/router.py`
6. Test: `tests/test_<name>.py`

## Kiểm tra code

```bash
make lint        # ruff
make typecheck   # mypy (strict)
make test        # pytest
make check       # cả 3
```

Test mặc định chạy trên SQLite in-memory + fakeredis (không cần Docker). Muốn chạy trên PostgreSQL thật:

```bash
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/ailusion_journey_test uv run pytest
```

(cần tạo database `ailusion_journey_test` trước.)
