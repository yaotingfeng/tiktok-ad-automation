#!/usr/bin/env bash
# 本地完整门禁：只在无私密配置的副本及独立 PG18/Redis8 实例上运行。
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)

# 仅继承工具连接配置，阻断终端中的生产数据库、云存储和平台凭据。
if [[ ${1:-} != --isolated-environment ]]; then
  clean_env=("PATH=$PATH" "HOME=$HOME")
  for key in TMPDIR LANG LC_ALL BUN_BIN PG_BIN_DIR DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG DOCKER_TLS_VERIFY DOCKER_CERT_PATH UV_CACHE_DIR UV_PYTHON_INSTALL_DIR PLAYWRIGHT_BROWSERS_PATH HTTPS_PROXY HTTP_PROXY ALL_PROXY NO_PROXY SSL_CERT_FILE SSL_CERT_DIR; do
    if [[ -n ${!key:-} ]]; then clean_env+=("$key=${!key}"); fi
  done
  exec env -i "${clean_env[@]}" bash "$repo/deploy/ci/check.sh" --isolated-environment
fi
cd "$repo"
for tool in python3 uv node docker ffprobe ffmpeg git; do
  command -v "$tool" >/dev/null || { echo "Missing required tool: $tool" >&2; exit 2; }
done
if [[ -z ${BUN_BIN:-} ]]; then
  BUN_BIN=$(command -v bun || true)
  if [[ -z $BUN_BIN ]]; then
    case "$(uname -s)-$(uname -m)" in
      Darwin-arm64) bun_platform=darwin-aarch64 ;;
      Darwin-x86_64) bun_platform=darwin-x64 ;;
      Linux-aarch64) bun_platform=linux-aarch64 ;;
      Linux-x86_64) bun_platform=linux-x64 ;;
      *) echo 'Set BUN_BIN to Bun 1.4.2 on this platform' >&2; exit 2 ;;
    esac
    BUN_BIN="$repo/.tools/bun-1.4.2/bun-$bun_platform/bun"
  fi
fi
[[ -x "$BUN_BIN" && $("$BUN_BIN" --version) == 1.4.2 ]] || { echo 'Bun 1.4.2 required; set BUN_BIN to its absolute path' >&2; exit 2; }
BUN_BIN=$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$BUN_BIN")
if [[ -z ${PG_BIN_DIR:-} ]]; then
  pg_candidates=()
  if command -v pg_config >/dev/null; then pg_candidates+=("$(pg_config --bindir)"); fi
  if command -v brew >/dev/null; then
    for formula in libpq@18 libpq; do
      prefix=$(brew --prefix "$formula" 2>/dev/null || true)
      [[ -z "$prefix" ]] || pg_candidates+=("$prefix/bin")
    done
  fi
  # 多版本客户端共存时不能因 PATH 中的旧版而错过已安装的 PG18。
  for candidate in "${pg_candidates[@]}"; do
    version=$("$candidate/pg_dump" --version 2>/dev/null || true)
    if [[ "$version" == *'PostgreSQL) 18.'* ]]; then PG_BIN_DIR="$candidate"; break; fi
  done
  if [[ -z ${PG_BIN_DIR:-} ]]; then
    echo 'Set PG_BIN_DIR to PostgreSQL 18 client bin directory' >&2; exit 2
  fi
fi
export PG_BIN_DIR
for tool in pg_dump pg_restore; do
  "$PG_BIN_DIR/$tool" --version | grep -Eq 'PostgreSQL\) 18\.' || { echo "PostgreSQL 18 $tool required" >&2; exit 2; }
done
docker info >/dev/null
mkdir -p "$repo/.runtime"
work=$(mktemp -d "$repo/.runtime/check-XXXXXXXX")
chmod 700 "$work"
run_id="tkada-ci-$(date +%s)-$$"
pg_name="$run_id-pg"
redis_name="$run_id-redis"
image_name="tiktok-ad-automation:ci-$run_id"
cleanup() {
  status=$?
  trap - EXIT
  # 名称属于本次唯一运行；失败也回收自建镜像、容器和源码副本。
  docker rm -fv "$redis_name" "$pg_name" >/dev/null 2>&1 || true
  docker image rm "$image_name" >/dev/null 2>&1 || true
  rm -rf "$work"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
python3 "$repo/deploy/ci/test_snapshot.py"
python3 "$repo/deploy/ci/snapshot.py" "$repo" "$work/source"
mkdir -p "$work/bin" "$work/source/.runtime"
ln -s "$BUN_BIN" "$work/bin/bun"
export PATH="$work/bin:$PATH"
cd "$work/source"
docker run -d --name "$pg_name" -p 127.0.0.1::5432 \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=ci-local-password -e POSTGRES_DB=tiktok_ci_test postgres:18 >/dev/null
docker run -d --name "$redis_name" -p 127.0.0.1::6379 redis:8 >/dev/null
for ((attempt=0; attempt<60; attempt++)); do
  if docker exec "$pg_name" pg_isready -U postgres >/dev/null 2>&1 && docker exec "$redis_name" redis-cli ping >/dev/null 2>&1; then break; fi
  sleep 1
done
docker exec "$pg_name" pg_isready -U postgres
docker exec "$redis_name" redis-cli ping
pg_port=$(docker port "$pg_name" 5432/tcp | sed 's/.*://')
redis_port=$(docker port "$redis_name" 6379/tcp | sed 's/.*://')
export DATABASE_URL="postgresql://postgres:ci-local-password@127.0.0.1:$pg_port/tiktok_ci_test"
export REDIS_URL="redis://127.0.0.1:$redis_port/0"
export TEST_REDIS_URL="redis://127.0.0.1:$redis_port/1"
export PROJECT_NAME='TikTok Ad Automation CI' FIRST_SUPERUSER=admin
export FIRST_SUPERUSER_PASSWORD=ci-only-superuser-password SECRET_KEY=ci-only-signing-key-not-for-deployment
export FASTAPI_ENV=development VITE_API_URL='' CI=1 APP_ENV_FILE=/dev/null DOTENV_CONFIG_PATH=/dev/null
export AWS_EC2_METADATA_DISABLED=true AWS_SHARED_CREDENTIALS_FILE=/dev/null AWS_CONFIG_FILE=/dev/null
export UV_NO_ENV_FILE=1 PLAYWRIGHT_SKIP_BROWSER_GC=1
uv sync --frozen --package app
node scripts/test-deployment-assets.mjs
(cd backend && uv run python -m tests.pg_backup && uv run ruff check app tests && uv run ty check app && uv run pytest -q --tb=short --show-capture=no)
bun install --frozen-lockfile
bun run --filter frontend build
(cd frontend && bun x playwright install chromium && bun x playwright test --project workspace --reporter=list && bun x playwright test --config=playwright.acceptance.config.ts && bun x playwright test --config=playwright.r2-acceptance.config.ts)
docker build -f backend/Dockerfile -t "$image_name" .
RELEASE_TEST_IMAGE="$image_name" uv run python scripts/test-release-integration.py
printf 'Full local check passed.\n'
