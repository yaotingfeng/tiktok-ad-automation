#!/usr/bin/env bash
# 本地完整门禁：只在无私密配置的副本及独立 PG18/Redis8 实例上运行。
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)

# 仅继承工具连接配置，阻断终端中的生产数据库、云存储和平台凭据。
if [[ ${1:-} != --isolated-environment ]]; then
  clean_env=("PATH=$PATH" "HOME=$HOME")
  for key in TMPDIR LANG LC_ALL BUN_BIN PG_BIN_DIR CHECK_PLAN CHECK_CACHE_DIR CHECK_BACKEND_SHARDS CHECK_ACCEPTANCE_SHARDS CHECK_BROWSER_WORKERS ACCEPTANCE_SCALE DOCKER_HOST DOCKER_CONTEXT DOCKER_CONFIG DOCKER_TLS_VERIFY DOCKER_CERT_PATH UV_CACHE_DIR UV_PYTHON_INSTALL_DIR PLAYWRIGHT_BROWSERS_PATH TKADA_IMAGE_MIRROR DEBIAN_MIRROR PYPI_INDEX_URL PYPI_FILES_URL NPM_REGISTRY HTTPS_PROXY HTTP_PROXY ALL_PROXY NO_PROXY SSL_CERT_FILE SSL_CERT_DIR; do
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
acceptance_pg_name="$run_id-acceptance-pg"
browser_pg_name="$run_id-browser-pg"
redis_name="$run_id-redis"
image_name="tiktok-ad-automation:ci-$run_id"
# 与 deploy/release/common.py 的 mirrored 一致：Jenkins 用本项目 ACR 副本，本机用官方镜像。
mirrored() { if [[ -n ${TKADA_IMAGE_MIRROR:-} ]]; then local ref=${1//\//-}; printf '%s%s' "$TKADA_IMAGE_MIRROR" "${ref//:/-}"; else printf '%s' "$1"; fi; }
cleanup() {
  status=$?
  trap - EXIT
  # 名称属于本次唯一运行；失败也回收自建镜像、容器和源码副本。
  docker rm -fv "$redis_name" "$pg_name" "$acceptance_pg_name" "$browser_pg_name" >/dev/null 2>&1 || true
  docker image rm "$image_name" >/dev/null 2>&1 || true
  rm -rf "$work"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
python3 "$repo/deploy/ci/test_snapshot.py"
python3 "$repo/deploy/ci/test_select_tests.py"
python3 "$repo/deploy/ci/test_backend_tests.py"
python3 "$repo/deploy/ci/test_slow_tests.py"
# 检查计划由 deploy/local.py 按改动生成；直接运行本脚本时默认全量。
plan_file=${CHECK_PLAN:-}
if [[ -z $plan_file ]]; then
  plan_file="$work/plan.json"
  python3 "$repo/deploy/ci/select_tests.py" --full --output "$plan_file"
fi
plan() { python3 -c 'import json,sys; v=json.load(open(sys.argv[1])); [v:=v[k] for k in sys.argv[2:]]; print(" ".join(v) if isinstance(v,list) else ("1" if v is True else ("" if v is False else v)))' "$plan_file" "$@"; }
mode=$(plan mode)
tier=$(plan tier)
printf '检查范围：%s（%s），层级：%s\n' "$mode" "$(plan reasons)" "$tier"
if [[ $mode == none ]]; then
  printf 'No checks required for this change.\n'
  exit 0
fi
backend_all=$(plan backend all)
backend_targets=$(plan backend targets)
lint=$(plan lint)
fe_build=$(plan frontend build)
fe_workspace=$(plan frontend workspace)
fe_batch=$(plan frontend batch)
fe_r2=$(plan frontend r2)
deploy_tools=$(plan deploy_tools)
release_integration=$(plan release_integration)
python3 "$repo/deploy/ci/snapshot.py" "$repo" "$work/source"
# 国内下载源只改源码副本里锁文件的下载地址，版本与哈希不变（与 backend/Dockerfile 相同做法）。
if [[ -n ${PYPI_INDEX_URL:-} ]]; then
  export UV_DEFAULT_INDEX=$PYPI_INDEX_URL
  python3 -c 'import os,pathlib; p=pathlib.Path(os.sys.argv[1]); t=p.read_text().replace("https://pypi.org/simple", os.environ["PYPI_INDEX_URL"]); f=os.environ.get("PYPI_FILES_URL"); p.write_text(t.replace("https://files.pythonhosted.org", f) if f else t)' "$work/source/uv.lock"
fi
if [[ -n ${NPM_REGISTRY:-} ]]; then export BUN_CONFIG_REGISTRY=$NPM_REGISTRY; fi
mkdir -p "$work/bin" "$work/source/.runtime" "$work/lanes"
ln -s "$BUN_BIN" "$work/bin/bun"
export PATH="$work/bin:$PATH"
cd "$work/source"
# 并行分组各用独立库与 Redis 库号；PG 连接数和 Redis 库数按并发放大。
# 测试库用完即删：数据放内存盘并关闭落盘保证，只影响本次临时实例，不涉及任何正式数据。
start_pg() {
  docker run -d --name "$1" -p 127.0.0.1::5432 --tmpfs /var/lib/postgresql:rw,size=4g \
    -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=ci-local-password -e POSTGRES_DB=tiktok_ci_test "$(mirrored postgres:18)" \
    -c max_connections=600 -c fsync=off -c synchronous_commit=off -c full_page_writes=off \
    -c shared_buffers=512MB -c work_mem=16MB >/dev/null
}
start_pg "$pg_name"
# 后端端到端验收用第二个实例、浏览器端到端验收用第三个实例：三类检查同时运行，
# 互不争用同一个数据库，浏览器验收无需等后端验收结束。
start_pg "$acceptance_pg_name"
start_pg "$browser_pg_name"
docker run -d --name "$redis_name" -p 127.0.0.1::6379 "$(mirrored redis:8)" --databases 64 >/dev/null
for ((attempt=0; attempt<60; attempt++)); do
  if docker exec "$pg_name" pg_isready -U postgres >/dev/null 2>&1 && docker exec "$acceptance_pg_name" pg_isready -U postgres >/dev/null 2>&1 && docker exec "$browser_pg_name" pg_isready -U postgres >/dev/null 2>&1 && docker exec "$redis_name" redis-cli ping >/dev/null 2>&1; then break; fi
  sleep 1
done
docker exec "$pg_name" pg_isready -U postgres
docker exec "$acceptance_pg_name" pg_isready -U postgres
docker exec "$browser_pg_name" pg_isready -U postgres
docker exec "$redis_name" redis-cli ping
pg_port=$(docker port "$pg_name" 5432/tcp | sed 's/.*://')
acceptance_pg_port=$(docker port "$acceptance_pg_name" 5432/tcp | sed 's/.*://')
browser_pg_port=$(docker port "$browser_pg_name" 5432/tcp | sed 's/.*://')
redis_port=$(docker port "$redis_name" 6379/tcp | sed 's/.*://')
export DATABASE_URL="postgresql://postgres:ci-local-password@127.0.0.1:$pg_port/tiktok_ci_test"
export ACCEPTANCE_DATABASE_URL="postgresql://postgres:ci-local-password@127.0.0.1:$acceptance_pg_port/tiktok_ci_test"
export REDIS_URL="redis://127.0.0.1:$redis_port/0"
export TEST_REDIS_URL="redis://127.0.0.1:$redis_port/1"
export PROJECT_NAME='TikTok Ad Automation CI' FIRST_SUPERUSER=admin
export FIRST_SUPERUSER_PASSWORD=ci-only-superuser-password SECRET_KEY=ci-only-signing-key-not-for-deployment
export FASTAPI_ENV=development VITE_API_URL='' CI=1 APP_ENV_FILE=/dev/null DOTENV_CONFIG_PATH=/dev/null
export AWS_EC2_METADATA_DISABLED=true AWS_SHARED_CREDENTIALS_FILE=/dev/null AWS_CONFIG_FILE=/dev/null
export UV_NO_ENV_FILE=1 PLAYWRIGHT_SKIP_BROWSER_GC=1
# 前端开发服务器与浏览器验收服务每批取空闲端口：并行的另一批检查或本机开发服务器
# 占用固定端口时，不会被本批测试复用或在中途关闭而导致成批连接失败。
read -r PLAYWRIGHT_PORT PLAYWRIGHT_SOURCE_PORT ACCEPTANCE_BROWSER_PORT R2_ACCEPTANCE_PORT R2_STORAGE_PORT < <(python3 -c '
import socket
socks = [socket.socket() for _ in range(5)]
for s in socks: s.bind(("127.0.0.1", 0))
print(*(s.getsockname()[1] for s in socks))
for s in socks: s.close()')
export PLAYWRIGHT_PORT PLAYWRIGHT_SOURCE_PORT ACCEPTANCE_BROWSER_PORT R2_ACCEPTANCE_PORT R2_STORAGE_PORT
uv sync --frozen --package app
# 测试数据库运行在 Docker 内，是所有分组共享的瓶颈：组数不超过 Docker CPU 数。
docker_cpus=$(docker info --format '{{.NCPU}}' 2>/dev/null || echo 4)
export CHECK_BACKEND_SHARDS=${CHECK_BACKEND_SHARDS:-$(( docker_cpus < 4 ? docker_cpus : (docker_cpus > 6 ? 6 : docker_cpus) ))}
cache_dir=${CHECK_CACHE_DIR:-$repo/.runtime/ci-cache}
mkdir -p "$cache_dir"

lane_backend() {
  cd backend
  uv run python -m tests.pg_backup
  if [[ -n $lint ]]; then uv run ruff check app tests && uv run ty check app; fi
  if [[ -n $backend_all || -n $backend_targets ]]; then
    local targets=(tests)
    # 目标路径不含空格，按空格拆分为多个参数。
    [[ -n $backend_all ]] || read -r -a targets <<<"$backend_targets"
    # 快速层排除 slow 标记的用例（含全部验收）；发布门禁 tier=all 全部运行。
    local tier_args=()
    [[ $tier == fast ]] && tier_args=(--fast)
    uv run python ../deploy/ci/backend_tests.py --work "$work/backend" ${tier_args[@]+"${tier_args[@]}"} \
      --durations ../deploy/ci/test-durations.json --durations "$cache_dir/test-durations.json" \
      --save-durations "$cache_dir/test-durations.json" "${targets[@]}"
  fi
}

lane_frontend() {
  bun install --frozen-lockfile
  bun run --filter frontend build
  cd frontend
  bun x playwright install chromium
  if [[ -n $fe_workspace ]]; then
    # 工作台用例只依赖页面内模拟接口，可并行；浏览器验收共用真实后端，保持串行。
    # 页面由 vite preview 提供构建产物（见 playwright.config.ts），多 worker 不再争用编译 CPU。
    bun x playwright test --project workspace --project workspace-source --reporter=list --workers="${CHECK_BROWSER_WORKERS:-3}"
  fi
  if [[ -n $fe_batch || -n $fe_r2 ]]; then
    # 浏览器端到端验收在工作台用例之后运行：与之重叠不缩短总时长（后端检查线更长），
    # 只会在共享节点上叠加内存占用。独立实例与独立 Redis 库号，与后端端到端验收同时运行。
    docker exec "$browser_pg_name" psql -U postgres -qc 'CREATE DATABASE ci_browser_test' >/dev/null
    export DATABASE_URL="postgresql://postgres:ci-local-password@127.0.0.1:$browser_pg_port/ci_browser_test"
    export REDIS_URL="redis://127.0.0.1:$redis_port/40" TEST_REDIS_URL="redis://127.0.0.1:$redis_port/41"
  fi
  if [[ -n $fe_batch ]]; then bun x playwright test --config=playwright.acceptance.config.ts; fi
  if [[ -n $fe_r2 ]]; then bun x playwright test --config=playwright.r2-acceptance.config.ts; fi
}

lane_deploy() {
  if [[ -n $deploy_tools ]]; then
    node scripts/test-deployment-assets.mjs
    TEST_RELEASE_REDIS_URL="redis://127.0.0.1:$redis_port/42" uv run python scripts/test-broker-migration.py
  fi
  if [[ -n $release_integration ]]; then
    source deploy/ci/image-build-args.sh
    image_build_args
    docker build -f backend/Dockerfile ${build_args[@]+"${build_args[@]}"} -t "$image_name" .
    RELEASE_TEST_IMAGE="$image_name" uv run python scripts/test-release-integration.py
  fi
}

# 三条检查线并行；各自日志结束后整段输出，失败时标明是哪一条。
lanes=()
[[ -n $lint || -n $backend_all || -n $backend_targets ]] && lanes+=(backend)
[[ -n $fe_build ]] && lanes+=(frontend)
[[ -n $deploy_tools || -n $release_integration ]] && lanes+=(deploy)
started=$SECONDS
pids=()
for lane in "${lanes[@]}"; do
  # 每条线自己记录耗时：按顺序等待时的累计时间会把先结束的线也算成最长那条。
  ( set -euo pipefail; lane_started=$SECONDS
    trap 'echo $(( SECONDS - lane_started )) >"$work/lanes/'"$lane"'.seconds"' EXIT
    "lane_$lane" ) >"$work/lanes/$lane.log" 2>&1 &
  pids+=($!)
  printf '检查线启动：%s\n' "$lane"
done
failed=()
for index in "${!lanes[@]}"; do
  lane=${lanes[$index]}
  if wait "${pids[$index]}"; then status=通过; else status=失败; failed+=("$lane"); fi
  seconds=$(cat "$work/lanes/$lane.seconds" 2>/dev/null || echo $(( SECONDS - started )))
  printf '检查线%s：%s（%d 分 %02d 秒）\n' "$status" "$lane" $(( seconds / 60 )) $(( seconds % 60 ))
done
for lane in "${lanes[@]}"; do
  printf '\n===== %s =====\n' "$lane"
  cat "$work/lanes/$lane.log"
done
if (( ${#failed[@]} )); then
  printf '\nCheck failed: %s\n' "${failed[*]}"
  exit 1
fi
printf '\nLocal check passed (%s, %s).\n' "$mode" "$tier"
