# Slice 0 — Docker / Dev Environment

**Depends on:** Nothing.
**Prereq:** Read `00-prompt-prefix.md` first.

---

## Goal

A single `docker compose up` brings up the entire stack. The developer needs only Docker installed on the host — no Python, no pip, no psql.

## Services

| Service | Image | Host port | Purpose |
|---|---|---|---|
| `postgres` | `pgvector/pgvector:pg16` | 5432 | PostgreSQL + pgvector extension |
| `redis` | `redis:7-alpine` | 6379 | ARQ job queue + cache |
| `minio` | `minio/minio:latest` | 9000, 9001 | Local S3-compatible storage |
| `minio-init` | `minio/mc:latest` | — | One-shot bucket creator |
| `api` | Python 3.12 (custom) | 8000 | FastAPI REST server |
| `worker` | same image as `api` | — | ARQ background worker |
| `ui` | Python 3.12 (custom) | 7860 | Gradio UI |

## Deliverables

- `docker-compose.yml` with all 7 services wired and healthy.
- `api/Dockerfile` — multi-stage (`base` → `dev` → `prod`).
- `web/Dockerfile` — single-stage for Gradio UI.
- `infra/postgres/init.sql` — enables pgvector extension.
- `infra/minio/init.sh` — creates the `kbchat-dev` bucket.
- `Makefile` with convenience targets.
- `.env.example` with all required variable names.
- `.env` committed for local dev (safe — all dummy/local values).

## `docker-compose.yml` structure

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: postgres
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./infra/postgres/init.sql:/docker-entrypoint-initdb.d/init.sql
    ports: ["5432:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      timeout: 5s
      retries: 5

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s

  minio:
    image: minio/minio:latest
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    ports: ["9000:9000", "9001:9001"]
    volumes: [minio_data:/data]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9000/minio/health/live"]
      interval: 10s

  minio-init:
    image: minio/mc:latest
    depends_on:
      minio: { condition: service_healthy }
    volumes: ["./infra/minio/init.sh:/init.sh"]
    entrypoint: ["/bin/sh", "/init.sh"]

  api:
    build:
      context: ./api
      target: dev
    command: uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
    volumes: ["./api:/app"]
    ports: ["8000:8000"]
    env_file: .env
    depends_on:
      postgres: { condition: service_healthy }
      redis: { condition: service_healthy }
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 10s

  worker:
    build:
      context: ./api
      target: dev
    command: arq src.worker.WorkerSettings
    volumes: ["./api:/app"]
    env_file: .env
    depends_on:
      postgres: { condition: service_healthy }
      redis: { condition: service_healthy }

  ui:
    build: ./web
    command: python app.py
    volumes: ["./web:/app"]
    ports: ["7860:7860"]
    env_file: .env
    depends_on:
      api: { condition: service_healthy }

volumes:
  postgres_data:
  minio_data:
```

## `api/Dockerfile`

```dockerfile
FROM python:3.12-slim AS base
WORKDIR /app
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml .
RUN pip install --no-cache-dir -e ".[dev]"

FROM base AS dev
# Source is bind-mounted at runtime

FROM base AS prod
COPY . .
```

## `web/Dockerfile`

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
```

## `infra/postgres/init.sql`

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

## `infra/minio/init.sh`

```sh
#!/bin/sh
mc alias set local http://minio:9000 minioadmin minioadmin
mc mb --ignore-existing local/kbchat-dev
```

## `Makefile`

```makefile
up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

psql:
	docker compose exec postgres psql -U postgres

redis-cli:
	docker compose exec redis redis-cli

sh-api:
	docker compose exec api bash

migrate:
	docker compose exec api alembic upgrade head

rebuild:
	docker compose down -v --remove-orphans
	docker compose build --no-cache
	docker compose up -d
```

## `.env` (committed for local dev)

```
# Database
DATABASE_URL=postgresql+asyncpg://postgres:postgres@postgres:5432/postgres

# Redis
REDIS_URL=redis://redis:6379

# S3 / MinIO
S3_ENDPOINT=http://minio:9000
S3_BUCKET=kbchat-dev
S3_REGION=us-east-1
S3_ACCESS_KEY_ID=minioadmin
S3_SECRET_ACCESS_KEY=minioadmin
MINIO_ROOT_USER=minioadmin
MINIO_ROOT_PASSWORD=minioadmin

# APIs — fill in real keys before running Slice 6 (embeddings) and Slice 8 (chat)
OPENAI_API_KEY=
ANTHROPIC_API_KEY=

# Auth — demo mode requires nothing else.
# To use real Clerk: set AUTH_MODE=clerk and fill in the two keys below.
AUTH_MODE=demo
CLERK_SECRET_KEY=
CLERK_WEBHOOK_SECRET=

# App
DASHBOARD_ORIGIN=http://localhost:7860
```

## Implementation notes

- `DATABASE_URL` uses the `postgresql+asyncpg://` scheme — SQLAlchemy async driver. The sync scheme (`postgresql://`) would break async operations.
- Named volumes are NOT needed for Python (no `node_modules` equivalent). The source bind-mount in dev is sufficient.
- The `api` and `worker` containers share the same Docker image but run different commands. This is intentional — they share the same codebase.

## Acceptance criteria

- `docker compose up -d` brings all services healthy (no crash loops).
- `curl localhost:8000/health` returns `{"status":"ok"}` (stubbed until Slice 1).
- MinIO console at `http://localhost:9001` shows `kbchat-dev` bucket (user: minioadmin / minioadmin).
- `docker compose exec postgres psql -U postgres -c "\dx"` lists `vector` extension.
- `docker compose down && docker compose up -d` restores state (Postgres volume persists).
