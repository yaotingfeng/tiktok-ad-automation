#!/usr/bin/env bash
# Jenkins Agent 预装 Python 3.14、uv、Bun 1.4.2、Node、Docker、ffmpeg 和 PG18 客户端。
set -euo pipefail
cd "$(dirname "$0")/../.."
for tool in uv bun node docker ffprobe; do command -v "$tool" >/dev/null; done
[[ $(bun --version) == 1.4.2 ]] || { echo 'Bun 1.4.2 required'; exit 2; }
: "${PG_BIN_DIR:?Agent must provide PostgreSQL 18 client directory}"
"$PG_BIN_DIR/pg_dump" --version
run_id="tkada-ci-$(date +%s)-$$"
pg_id=''
redis_id=''
cleanup() {
  [[ -z "$redis_id" ]] || docker rm -fv "$redis_id" >/dev/null
  [[ -z "$pg_id" ]] || docker rm -fv "$pg_id" >/dev/null
}
trap cleanup EXIT
pg_id=$(docker run -d --name "$run_id-pg" -p 127.0.0.1::5432 \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=ci-local-password -e POSTGRES_DB=tiktok_ci_test postgres:18)
redis_id=$(docker run -d --name "$run_id-redis" -p 127.0.0.1::6379 redis:8)
for attempt in $(seq 1 60); do
  if docker exec "$pg_id" pg_isready -U postgres >/dev/null && docker exec "$redis_id" redis-cli ping >/dev/null; then break; fi
  sleep 1
done
docker exec "$pg_id" pg_isready -U postgres
docker exec "$redis_id" redis-cli ping
pg_port=$(docker port "$pg_id" 5432/tcp | sed 's/.*://')
redis_port=$(docker port "$redis_id" 6379/tcp | sed 's/.*://')
export DATABASE_URL="postgresql://postgres:ci-local-password@127.0.0.1:$pg_port/tiktok_ci_test"
export REDIS_URL="redis://127.0.0.1:$redis_port/0"
export TEST_REDIS_URL="redis://127.0.0.1:$redis_port/1"
export PROJECT_NAME='TikTok Ad Automation CI' FIRST_SUPERUSER=admin
export FIRST_SUPERUSER_PASSWORD=ci-only-superuser-password SECRET_KEY=ci-only-signing-key-not-for-deployment
export FASTAPI_ENV=development VITE_API_URL=''
uv sync --frozen --package app
node scripts/test-deployment-assets.mjs
(cd backend && uv run python -m tests.pg_backup && uv run ruff check app tests && uv run ty check app && uv run pytest -q --tb=short --show-capture=no)
bun install --frozen-lockfile
bun run --filter frontend build
(cd frontend && bun x playwright install chromium && bun x playwright test --project workspace && bun x playwright test --config=playwright.acceptance.config.ts && bun x playwright test --config=playwright.r2-acceptance.config.ts)
docker build -f backend/Dockerfile -t "tiktok-ad-automation:ci-$run_id" .
RELEASE_TEST_IMAGE="tiktok-ad-automation:ci-$run_id" uv run python scripts/test-release-integration.py
docker image rm "tiktok-ad-automation:ci-$run_id" >/dev/null
