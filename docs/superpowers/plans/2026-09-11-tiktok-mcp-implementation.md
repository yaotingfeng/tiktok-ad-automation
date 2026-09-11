# TikTok 官方 MCP 双通道集成 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 用户对 Superpowers 按需使用的偏好优先；执行方式不改变技术验收或授权边界。

**Goal:** 租户管理员自行完成官方 MCP 授权后，在没有本产品 TikTok App 配置时完成账户发现、视频上传、广告搭建及结果回读，同时正式支持现有 SDK 通道。

**Architecture:** 在现有 FastAPI/Celery 后端引入统一 TikTok 业务操作契约，由固定连接选择官方 SDK 或官方 MCP 适配器。权限、冻结预览、持久化步骤、outbox 和结果未知核查留在业务层；MCP 仅由代码直接调用，不运行 Agent。先完成协议基础与独立授权，再核实全部只读依赖，最后开放素材和广告副作用。

**Tech Stack:** Python >=3.14、FastAPI、SQLModel/Alembic、PostgreSQL、Redis/Celery、固定版本 TikTok Python SDK、官方 MCP Python SDK、React/TypeScript/shadcn/ui、Bun、pytest、Playwright。

**Spec:** [2026-09-11-tiktok-dual-channel-mcp-design.md](../specs/2026-09-11-tiktok-dual-channel-mcp-design.md)，用户于 2026-09-11 以“没问题”确认书面设计。

## Global Constraints

- `OFFICIAL_API` 使用现有固定版本 TikTok Python SDK，`OFFICIAL_MCP` 使用固定版本官方 MCP Python 客户端。
- MCP 由 Python 后端直接调用，工具、参数和顺序均由代码确定。
- 一个租户可以管理多个 BC；每个 MCP 连接明确绑定一个 BC。
- 租户管理员自行授权；凭据、连接、BC、账户及任务始终带租户归属。
- 三级广告直接以 `ENABLE` 创建；所有剧目投向全部输入账户；上传不绑定剧目。
- 正常 token 刷新只更新 `credential_revision`；授权语义变化才更新 `authorization_revision`。
- 冻结路由包括连接、通道、BC、授权语义版本、业务契约版本；不保存 token 副本。
- 结果未知后不再次发送同一副作用请求；本地退出/租约到期不证明远端取消。
- R2 原件在远端可能读取或结果未知时有持久化用途保护；当前 1 GiB 是应用容量，不是已验证的 MCP 上传限制。
- TikTok 官方 MCP endpoint 为 `https://business-api.tiktok.com/open_mcp/tt-ads-mcp-flat`；不新增任意 MCP URL 或自建转接服务。
- 所有实施与审查代理按本仓库 AGENTS 使用 `gpt-6-astra`、`high`；不自动创建分支，不自动推送或创建合并请求。
- 真实数据库/Redis 验证并发，外部替身在传输边界；凭据、运行环境、真实账户清单和视频不进 Git。

## 1. 计划文件与执行顺序

| 计划 | 内容 | 依赖 |
| --- | --- | --- |
| [P0 协议与公共调用边界](2026-09-11-tiktok-mcp-p0-protocol.md) | 公共协议、上下文/结果、MCP 会话、共享准入 | 已批准设计 |
| [P1 连接与授权](2026-09-11-tiktok-mcp-p1-connections.md) | 迁移、租户授权/刷新、BC 绑定/默认路由、账户/场景读取、连接页面 | P0 公共合同 |
| [P2 素材](2026-09-11-tiktok-mcp-p2-materials.md) | 素材读取、R2 入库、封面、目标分发、未知恢复和清理 | P1 路由/授权/工厂；写入前完成只读里程碑 |
| [P3 广告](2026-09-11-tiktok-mcp-p3-builds.md) | 广告只读契约、冻结上下文、CTA/三级创建、核查、UI、集成验收 | P1 工厂；真实创建依赖 P2 素材完整闭环 |

每份计划按业务职责归档，不要求整个文件一次性执行完。为满足设计中“先完整只读，再副作用”的顺序，具体执行为：

1. P0 公共协议、结果类型、传输与准入全部完成。
2. P1 Task 1–6 完成连接模型、授权/刷新、选路、账户及工厂；新 MCP 连接仍只读。
3. P2 Task 1 与 P3 Task 1 补齐素材及广告只读契约，可按独立文件并行；工厂注册由 root 顺序集成。随后前置执行 P3 Task 2，仅保存预览/提交的父路由和历史伴随上下文，不开启创建。
4. P1 Task 7–8 完成场景任务及页面；场景执行调用方此时可直接加载已落库的父路由。验收完整只读里程碑：无本产品 App 配置、租户自助授权、BC/账户/Identity/Minis/CTA/地区/VBO、素材和广告读取，以及官方实际 schema/权限事实；缺少某策略依赖则明确不可提交。
5. P2 Task 2 先固定源/目标路线并同步全部素材准备调用方，再执行 Task 3 URL 写入、Task 4 封面及 Task 5 未知恢复/清理保护。
6. P3 Task 3–6 完成创建、恢复、UI 和跨通道集成任务；文档/离线验收完成后再做明确授权的目标环境联调。

P0 不把“独立授权后的真实 tools/list”设为实现 P1 的前置，否则形成循环依赖。P0 收集公开 metadata 和文档预期合同，P1 授权后发布真实连接级观察证据；素材/广告写能力在后续阶段通过前保持关闭。

## 2. 文件与接口所有权

| 所有者 | 文件职责 |
| --- | --- |
| P0 | `integrations/tiktok/contracts/context.py`、`common.py`、`mcp/protocol.py`、`results.py`、`transport.py` 与公共准入 |
| P1 | `modules/accounts/` 的连接/授权/绑定/路由，`contracts/accounts.py`、`scenes.py`，账户和场景两套适配器，连接页面 |
| P2 | `contracts/materials.py`、SDK/MCP 素材适配器、`modules/materials/` 的来源/目标路线、上传/封面/恢复/清理 |
| P3 | `contracts/builds.py`、SDK/MCP 广告适配器、`modules/builds/` 的冻结/执行/回读/恢复与任务 UI |
| root 集成 | `integrations/tiktok/gateway.py`、配置、依赖锁、API/Celery 注册、生成客户端、Alembic 迁移链和进度 |

`modules/providers` 为版权方模块，不作为 TikTok gateway。`gateway.py` 在 P1 首次加入 accounts/scenes，在 P2 首任务加入 materials，在 P3 首任务加入 builds，不能提前 import 尚不存在的后续类型。

统一公共签名如下，阶段计划不能私自改名或改变版本语义：

```python
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict
from redis import Redis
from sqlalchemy import Engine
from sqlmodel import Session
from app.core.context import TenantContext

class FrozenTikTokRoute(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    tenant_id: UUID
    bc_id: str
    connection_id: UUID
    channel: Literal['OFFICIAL_API', 'OFFICIAL_MCP']
    authorization_revision: int
    adapter_contract_revision: str

def freeze_route(session: Session, *, context: TenantContext, bc_id: str,
                 connection_id: UUID | None = None) -> FrozenTikTokRoute: ...

def verify_route(session: Session, *, context: TenantContext, route: FrozenTikTokRoute,
                 advertiser_id: str | None,
                 capability: Literal['read', 'upload', 'build']) -> None: ...

def open_tiktok_gateway(*, database_engine: Engine, redis_client: Redis,
                       context: TenantContext,
                       route: FrozenTikTokRoute,
                       task_deadline: datetime) -> AbstractContextManager['TikTokGateway']: ...
```

以上是接口声明，不是待填函数实现。`FrozenTikTokRoute` 定义于 P0；freeze/verify 于 P1 `modules/accounts/routing.py`；`TikTokGateway` 及 factory 于 P1 `integrations/tiktok/gateway.py`，各组类型来自相应阶段契约。路由用 `model_dump(mode='json')` 持久化及 `model_validate` 严格还原。

`CallEvidence`、`McpBusinessResponse`、`RemoteCallError` 定义见 P0.2；证据为 frozen dataclass，不能误用 Pydantic 的 model_dump。`BoundMCPClient.call` 定义见 P0.3；每次调用的授权/准入由 P1 factory 注入，结果未知统一保留 `effect='UNKNOWN'`。`task_deadline` 是本次执行的 UTC 绝对期限，调用方在任务入口确定，工厂、刷新检查和各次调用只能缩短；不能从静态协议 profile 推导，也不在每次建会话时重新延长。素材使用现有 RemoteCallBudget.deadline；广告取本次 worker 期限与 claim.lease_expires_at 的较早值。

## 3. 环境、测试与迁移的执行规则

所有命令在应用独立仓库运行。开始及提交前检查 `git status -sb` 与 `git rev-parse --show-toplevel`；当前起点为 `f481134`，既有 `docs/design-history/` 未跟踪目录不属于本任务。没有已有可用隔离分支时不自动新建；可顺序实施当前分支，独立代理只做明确文件范围的工作。

测试使用专属 PostgreSQL 数据库和独立非零 Redis DB，由执行者配置 `DATABASE_URL`、`TEST_REDIS_URL`，不能使用开发/生产库或打印 DSN。根目录执行以下只验证目标类型的命令，先通过再运行任何迁移：

```bash
cd backend
uv run python -c 'import os; from app.core.config import settings; from tests.database import require_test_database, require_test_redis; require_test_database(str(settings.DATABASE_URL)); require_test_redis(os.environ["TEST_REDIS_URL"], settings.REDIS_URL)'
```

现有 `backend/tests/conftest.py` 自动迁移测试库。并发用例需要独立连接提交的 fixture，不能用一个外层回滚事务冒充并发。新测试按既有权限 fixture 创建真实 tenant/user/membership；accounts 的 autouse App fixture 会填假配置，MCP 无 App 测试必须显式清空三个字段。

已有可靠命令入口：

```bash
# backend 工作目录，按阶段选择对应的测试路径
uv run pytest tests/integrations/tiktok -q
uv run ruff check app/integrations/tiktok tests/integrations/tiktok
uv run mypy app/integrations/tiktok
uv run ty check app/integrations/tiktok

# 应用仓库根目录
bash scripts/generate-client.sh
bun run --filter frontend build

# frontend 工作目录，使用现有 workspace 配置及 API 边界替身
bunx playwright test --project workspace tests/tenants-accounts.spec.ts --reporter line
```

`frontend/package.json` 的 lint 会写入且带 unsafe，不把它当只读检查使用；使用 `bunx biome check` 加本轮明确文件。新增 Playwright 文件若不在 workspace 的 testMatch 中，必须更新 `frontend/playwright.config.ts` 的匹配和其他项目排除规则，避免落入真实登录 setup。

迁移实际顺序由 root 集成：P1 连接/授权/默认路由（`mcp01`）→ 目录暂存页（`mcp_stage_directory`）→ 权限任务授权路由（`mcp_capability_routes`）→ 暂存页 BC 范围（`mcp_directory_bc_scope`）→ P3 广告父上下文（`mcp_build_routes`）→ P1 场景路由（`mcp02`）→ 目录语义版本（`mcp_directory_semantics`）→ P2 素材上下文（`mcp_material_routes`）→ P3 核查授权（`mcp_build_recovery`）→ 草稿显式连接选择（`mcp_draft_connection`）→ 封面原摘要与回执事实（`mcp_cover_evidence`）。每次生成前以当前 `uv run alembic heads` 确认上游；由 Alembic 从实际单一 head 生成 down_revision，不猜上游 hash、不改旧迁移。生成文件名以命令输出为准并同步计划。验证旧连接归类、唯一默认/复合外键、历史不可变请求不被更新，再运行测试库 upgrade/check 与独立迁移测试。

本地全量后端、前端和真实 Linux prefork 检查安排在 P3 集成任务；此前每任务执行相关回归，不重复跑无关全量。每个独立测试周期后聚焦提交，明确路径暂存、检查暂存差异、更新 `docs/implementation-progress.md`，不 `git add .`。

## 4. 用户操作与真实联调的边界

规划和本地离线实现不需要反复确认。以下真实操作必须在结果可审阅且目标明确时处理：

- 独立 MCP 客户端注册/授权：准备好回调地址、目标环境、租户入口和权限范围后，由租户管理员在系统中发起。无需复制 Codex 的已登录会话。
- 实际源素材上传/目标分发/广告试投：列出选定 BC、后端连接、账户、素材和具体预览/预算；用户提交该实际操作后再发送。运营工具的骏伯/星屿连接不得混用。
- 发布：依据 `docs/runbooks/deployment.md` 及目标环境手册形成迁移/备份/排空/回滚清单；本地测试不授权部署，不视为生产可用。

若官方注册或权限证据存在当前不可获得的条件，先记录该条件及受影响能力；不重新要求填写尚未获批的 Marketing API App 来掩盖问题。其余不依赖该条件的离线任务继续推进。

## 5. 设计覆盖与交付状态

| 设计要求 | 对应任务 |
| --- | --- |
| §1–4 双通道、固定官方服务、共享 DTO/工厂 | P0.1–P0.3；P1 工厂与账户/场景；P2/P3 首个契约任务 |
| §5 自助授权、候选发布、刷新、撤销与权限变化 | P1 授权/刷新/连接页面任务 |
| §6 固定路由、默认切换、账户能力 | P1 路由/发现任务；P2 来源与目标上下文；P3 冻结及派生任务 |
| §7 完整只读及工具契约 | P0.1、P1 实际 schema/账户/场景、P2/P3 首个只读任务 |
| §8 R2、实际来源/目标、容量和用途保护 | P2 入库/分发/封面/清理测试 |
| §9 直接 ENABLE、回执、UNKNOWN、配额 | P0.2–P0.4；P3 创建/回读/恢复；P2 上传 UNKNOWN |
| §10 迁移、历史保持、环境发布 | P1/P2/P3 各自迁移任务及 P3 集成/发布验收 |
| §11 全部验收行为 | 各阶段红绿测试与 P3 双通道集成矩阵 |

- [x] 用户确认书面设计。
- [x] 编写分阶段实施计划并完成文档检查。
- [x] 完成 P0 协议模型与公共调用边界的本地实现和验证，未获真实服务证据的能力保持关闭。
- [x] 完成 P1 授权/账户/场景及 P2/P3 首任务组成的本地只读里程碑。
- [x] 完成 P2 素材入库、目标分发、封面和未知原件保护的本地实现与定向验证。
- [x] 完成 P3 广告执行、只读恢复及连接页面的本地实现与定向验证。
- [x] 完成本机跨阶段后端最终矩阵：2203 项通过、9 项跳过（8 项 Linux、1 项 API 分支不适用），1304.32 秒；前端 workspace 348 项与构建通过。
- [ ] 在实际 Linux 环境完成 prefork 终止与恢复验收，不能以 macOS 跳过代替。
- [ ] 核实真实 MCP 注册、授权主体/权限、工具响应及视频服务策略，逐项登记联调证据。
- [ ] 按具体授权完成目标环境联调/发布，分别记录证据。

代码、审查和提交按 [实施进度](../../implementation-progress.md) 登记；完整矩阵与环境限制见 [离线验收](../../validation/2026-09-11-tiktok-dual-channel-offline.md)。本地实现、真实服务联调和生产发布分别核实，不将本地合成结果记为实际服务成功。
