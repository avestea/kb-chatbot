FROM python:3.12-slim AS base
WORKDIR /app
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml .
RUN mkdir -p src && pip install --no-cache-dir -e ".[dev]"

FROM base AS dev
# Source is bind-mounted at runtime

FROM base AS prod
COPY . .
