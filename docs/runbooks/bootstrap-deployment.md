# 本地运行与回调入口部署

2026-09-10 生产环境已建立，目标为 `https://manjuad.gzjunbo.net:8000`。该环境的部署、更新、备份和数据库迁移以 [骏伯生产规则](production-junbo.md) 为准；下列本地/staging 命令不用于该生产主机。基础验收与尚未完成的外部联调分别见 [生产发布记录](../validation/2026-09-10-production-release.md)。

## 当前验证状态（2026-09-09）

2026-09-10更新：普通账号密码及租户版权方自动续登已升级，本地库进一步升级至 `r2_part_receipts`，已验证备份恢复及原记录保留。R2批量上传、源入库后清理和原件删除后的目标分发实现与本地/外部验收边界见[实施进度](../implementation-progress.md)及[R2运行手册](r2-video-upload.md)。主目录已重建并重启8011；新导入、自动清理两个开关保持关闭，不能用本地solo实例验收生产素材任务。

本地真实 PostgreSQL 17.5 / Redis 8 已验证迁移、管理员登录、受保护用户接口、静态页面、回调业务响应以及后台投递。应用功能、两版权方完整离线搭建、浏览器和大量账户验收见[功能与页面交付](../acceptance/functional-delivery.md)、[离线记录](../acceptance/offline.md)和[容量记录](../acceptance/capacity.md)。

本地 origin 为 `http://127.0.0.1:8011`；它没有公网或 HTTPS 含义。此前 Compose 两套配置已校验，GitHub CI 已实际构建镜像。上述本地验证当时未涉及生产主机；当前生产地址与验收见本页顶部链接。真实 OAuth 与真实广告创建仍按[外部联调清单](../acceptance/live-sdk.md)执行。

当前应用根目录为 `/Users/yaotingfeng/Documents/ytf/ytf-os-ad-skill/projects/tiktok-ad-automation/`。2026-09-09 迁移时保留了独立 Git 历史、worktree、私有配置及 PostgreSQL / Redis / Beat 数据；重建主虚拟环境并从新目录重启了服务。不要继续使用旧的同级目录启动命令。

## 首次使用与可用范围

1. 打开当前本地地址 `http://127.0.0.1:8011`，用本机私有 `.env` 的 `FIRST_SUPERUSER` 与 `FIRST_SUPERUSER_PASSWORD` 登录。凭据不会放入文档或 Git。
2. 平台管理员先在“平台租户管理”中点击某一行的“进入租户”。平台范围只提供租户和用户管理；业务菜单在明确进入租户后出现，不会自动选择第一个租户。
3. 在租户内，可以先保存投放策略。平台管理员或租户管理员还可以配置版权方连接、管理成员；这些入口不依赖 BC 授权。版权方实际验证与取链需要该租户自己的有效凭据。
4. 素材、搭建和任务页面在没有 BC 时显示连接说明，并提供已可使用的配置入口。App 尚未配置时，账户授权页面说明缺失配置；不能用示例 BC 或假授权使真实操作放行。

当前宿主机 Worker 使用 macOS `solo` 模式，仅用于基础运行诊断。需要硬截止的能力、Scene、素材和广告任务会拒绝在该模式下执行。完整任务执行必须按下方 Compose / Linux prefork 配置运行；外部投放还需要对象存储、App、租户授权和版权方凭据，并完成真实联调。页面及离线自动化测试通过不能替代这些验收。

## 配置与依赖

依赖 Python 3.14、uv、Bun 1.4.2、PostgreSQL 18（本地现有 17.5 已测）、Redis 8；容器方案需要 Docker Engine 与 Compose v2 或更新版本。根目录 `uv.lock`、`bun.lock` 均须冻结安装。

复制 `.env.example` 为 `.env`，填写数据库、Redis、独立随机签名密钥及初始管理员密码。`POSTGRES_PASSWORD` 用于 Compose 数据库，优先使用 URL 安全随机字符。`DATABASE_URL` 用于宿主机运行；Compose 内使用服务名自动生成连接地址。需要不同开发实例时分别提供数据库与 Redis 端口。

不要启用额外的请求参数或错误上报采集：回调查询包含授权码和 state。API 启动必须使用 `--no-access-log`；当前 Traefik 不开 access log，继承的 Sentry 自动初始化已移除。业务日志使用允许字段列表。`.env`、运行目录、worktree 与登录会话均已从 Git 和 Docker 构建上下文排除。

未取得 TikTok App 时保留相关字段为空；回调返回 `tiktok_app_not_configured`。部分配置返回缺失字段名。Fernet、对象存储及调用额度须在开放相应业务前配置并验收，不用示例凭据放行真实调用。即使尚无 API App，只要启用 MCP，也必须提前配置非空 `TIKTOK_CALL_POLICIES`；按照 [调用额度配置与交付门槛](deployment.md#调用额度配置与交付门槛) 完成解析、租约、服务加载及授权后读取检查。基础启动成功不表示外部集成已可用。

## 宿主机启动

从仓库根目录执行依赖安装及前端构建：

```bash
uv sync --frozen --package app
bun install --frozen-lockfile
bun run --filter frontend build
```

然后在 `backend/`：

```bash
uv run alembic upgrade head
uv run python app/initial_data.py
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

另外两个终端在 `backend/` 运行。只启动一个 Beat 实例；Linux 部署 Worker 使用默认 prefork，macOS 本地基础诊断可加 `--pool solo`，不能以 solo 结果证明任务硬截止：

```bash
uv run celery -A app.jobs.celery_app:celery_app worker -Q resources,builds,control --loglevel INFO
uv run celery -A app.jobs.celery_app:celery_app beat --schedule ../.runtime/celerybeat-schedule --loglevel INFO
```

Beat 本地运行前创建 `.runtime/`。开发前端可另起 `bun run dev`，默认把 `/api` 转发至 `127.0.0.1:8000`；其他端口通过启动命令的 `API_PROXY_TARGET` 指定。生产 UI 与 API 同源，不需要设置浏览器 API 密钥。

运行入口检查（`backend/`）：

```bash
uv run python ../scripts/check-bootstrap.py http://127.0.0.1:8000
```

脚本只检查健康、静态登录页、无授权参数回调的确定业务错误，以及未知 API 返回 404。它不会登录、换码或创建广告，成功不代表 OAuth 已成功。`/login` 的检查使用 `Accept: text/html`，与真实浏览器导航一致。

## 测试与客户端生成

测试必须指向独立名称包含 `_test` 的 PostgreSQL 数据库，`TEST_REDIS_URL` 指向不同于业务 Redis 的非零 DB。测试启动前自动检查数据库名称，再迁移、使用事务回滚；并发测试自行管理专用数据。不要把业务库改名为测试库来绕过检查。

```bash
# 在 backend/ 中，先通过环境配置指定独立测试数据库与 Redis。
uv run pytest -q
uv run ruff check app tests
uv run ty check app
```

生成客户端用根目录 `bash scripts/generate-client.sh`。脚本从真实 OpenAPI 生成类型，不自动修改其他前端文件。前端构建 `bun run --filter frontend build`；工作台测试在 `frontend/` 执行 `bun x playwright test --project workspace`（先安装 Chromium），外部边界由测试拦截。

## 本地 Compose

```bash
docker compose -f compose.yml -f compose.override.yml config --quiet
docker compose -f compose.yml -f compose.override.yml up -d --build
```

本地端口仅绑定 loopback：后端 8000、PostgreSQL 55432、Redis 56379。prestart 成功完成迁移和初始用户后才启动 API、Worker、Beat。不要使用 `down -v` 停止有业务数据的实例；正常停止用 `stop` 或不带 `-v` 的 `down`。

## 实际 HTTPS 预发布

取得真实域名和服务器后，配置 DNS、防火墙 80/443，并在私有环境文件中填写 `DOMAIN`、`ACME_EMAIL`、`FRONTEND_HOST=https://实际域名`。复制 `deploy/traefik.example.yml` 为被忽略的 `deploy/traefik.yml`，把 Host 规则改成同一个实际域名。代理使用文件配置，无 Docker socket 或 dashboard。

**必须显式使用以下两个文件**，避免自动混入本地 override：

```bash
docker compose -f compose.yml -f compose.staging.yml config --quiet
docker compose -f compose.yml -f compose.staging.yml up -d --build
```

预发布仅代理暴露 80/443，DB、Redis、后端不向主机发布端口。API/Worker/Beat 使用相同镜像；Beat 只部署一个，PostgreSQL、Redis、证书与 Beat 使用持久卷。实际部署需记录镜像版本、数据备份和回滚版本。

对实际 HTTPS origin 执行检查，人工验证管理员登录。随后从实际配置填写 `TIKTOK_REDIRECT_URI=https://实际域名/api/integrations/tiktok/callback`，用于申请开发者 App。该路径示例不能当成已部署地址。App 创建后设置 App ID/secret/授权门户 URL 与核实后的调用额度，再执行租户授权验收。

## 初次启动证据（历史）

验证 origin 为 `http://127.0.0.1:8010`，不具有公网或 HTTPS 含义。2026-09-09：健康检查、静态登录、回调 503 稳定错误、未知 API 404 全部通过；实际初始化管理员登录及受保护 profile 通过。持久化 no-op `jobs.probe` 经 Beat → Worker 消费成功，outbox `published_at` 已写且 attempts=1。全部使用本地实例，没有 TikTok/版权方调用。
