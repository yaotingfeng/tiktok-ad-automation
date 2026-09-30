# 广告管理与报表总路线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付覆盖全部授权账户广告的六维管理与报表，支持 MCP/API 双通道、持续同步和可追踪的批量 ROAS、启停与预算操作。

**Architecture:** 在已有多租户/BC 和后台任务基础上，新增 ads 目录、reporting 数据与查询、ad_management 写入任务三个模块。目录/指标在完整采集后发布，本地查询冻结结果，管理任务冻结影响范围并通过平台适配器执行。

**Tech Stack:** Python >=3.14、uv、FastAPI、SQLModel、Alembic、PostgreSQL、Redis、Celery、官方 TikTok Python SDK/官方 MCP、React、TanStack、shadcn/ui、Bun、Playwright。

**Spec:** [广告管理与报表一体化设计](../specs/2026-09-30-ads-management-reporting-design.md)，用户已于 2026-09-30 明确确认。当前状态：实施计划待用户审阅和选择执行方式，尚未实施。

## Global Constraints

- 系列名统一按 `版权方-剧名-其他备注` 识别，剧名在第二段。
- 当前 BC 内按“版权方＋剧名”归组，系列下级继承此归属。
- 不区分原剧名和改名后的剧名，不建立人工剧关联流程或独立业务剧目录。
- 本模块不包含分成、返点、赠款、赔付及经营净收益指标。
- 广告完整详情每 3 小时同步，核心报表每 30 分钟主动拉取。
- 普通与外部创建广告同等入库；广告身份不包含连接或通道，访问和冻结任务始终保留租户/BC范围。
- “预览有效期 5 分钟，超时或关键配置变化重新准备。” “预览提交一次即授权该具体任务，不增加第二套激活流程。”
- 正常明确成功回执直接记为接口已成功接受/更新，不逐项串行阻塞回读；结果不明先核实。
- API 和 MCP 适配器同时实现与离线测试；缺授权的真实联调独立标未完成，不以替身测试宣称已接通。
- 所有工作在本独立仓库完成，不新建分支、切换工作区或自动创建合并请求；仅本地提交，推送/部署另按用户授权范围执行。
- 如执行期间基线变化，保留他人改动并调整本轮新文件衔接，不能清空队列、重写历史迁移或丢弃用户更改。

## Review Focus

- 普通视频没有 ad_material_id，仍可读报表，但独立启停必须显示不支持：A3、B2、C3。
- 15 分钟查询快照与 5 分钟管理预览、任务持久副本的寿命不同：B3、C3/C4。
- 报表修正/系列改名发生在翻页和导出之间，不能混版本或把迁组算新增消耗：A5、B2/B3/B4。
- 已有 build 权限不代表新的管理权限，旧隔离写入也必须参与同对象协调：C1/C2/C4。
- 发布配置缺消费者、真实额度或启用值时不能把本地通过写成部署完成：A8、下面的集成验收步骤。

## 1. 阶段与完成边界

| 顺序 | 计划 | 独立产出 | 依赖 |
| --- | --- | --- | --- |
| A | [目录、双通道与采集](2026-09-30-ads-reporting-a-data.md) | 正确的广告/素材目录、平台报告事实、可恢复后台同步；8 项任务 | 现有账户授权、SDK/MCP、Outbox |
| B | [六维工作台](2026-09-30-ads-reporting-b-workspace.md) | 六维列表/汇总/趋势/详情、搜索、选择、个人视图和导出；6 项任务 | A 的稳定类型、目录和事实 |
| C | [批量管理](2026-09-30-ads-reporting-c-management.md) | ROAS/预算/启停预览、管理任务、审计/恢复；7 项任务 | A 的平台身份/刷新、B 的冻结选择 |

共 21 个任务，每项具备明确文件、接口、失败测试、实现、通过验证和聚焦提交。阶段 A 可独立验证采集，B 可独立交付只读工作台，C 完成管理闭环；不能在 B 完成时声称所有批量能力已经交付。

## 2. 文件所有权及统一接口

根端负责共同合同、迁移链、权限注册、Gateway、Celery/任务路由、部署模板、OpenAPI/前端路由生成、整体验收与进度记录。领域工作按各阶段 Files 分配；共享文件由根端顺序整合，不让两个执行者并发改同一文件。不需要为了计划创建工作树；执行若要隔离，遵循 AGENTS.md，只使用绑定已有分支的独立工作树并事先说明。

| 提供方 | 精确合同 | 使用方 |
| --- | --- | --- |
| A1 `contracts/ads.py` | `EntityRef(tenant_id, advertiser_id, kind, remote_id)`；kind 为 campaign/adgroup/ad/creative | A/B/C，共享稳定身份 |
| A1 `contracts/ads.py` | `MaterialUseRef(ad_ref, platform_material_id, ad_material_id: str\|None, material_type)` | 目录、素材视图、素材操作；None不允许替换为VID |
| A1 `ads/naming.py` | `parse_campaign_name(name:str)->CampaignIdentity` | A发布名称，B/C消费结果，不再解析一遍 |
| A2 `ads/directory.py` | `locate(session,*,context,bc_id,ref)->AdObject`、`list_objects(session,*,context,bc_id,advertiser_ids,kind,parent=None)->tuple[AdObject,...]` | B详情及C影响展开 |
| A7 `reporting/scheduling.py` | `request_sync(session,*,context,request:SyncRequest)->UUID` | B显式刷新/回补，C成功后定向刷新 |
| B3 `reporting/selection.py` | `freeze_selection(session,*,context,bc_id,request:SelectionRequest)->FrozenSelection` | C创建预览 |
| C3 `ad_management/previews.py` | `prepare_preview(session,context,bc_id,selection_id,mutation)->ManagementPreviewPublic` | 管理预览API/UI |
| C4 `ad_management/submissions.py` | `submit_management_task(session,context,preview_id,preview_digest,idempotency_key:UUID)->ManagementTaskPublic` | 提交API/UI |

`FrozenSelection` 具有 selection_id、snapshot_id、refs、material_uses、membership_digest、expires_at。账户/剧已展开快照时的系列；素材既保留广告 ref 又保留使用位置。C 只展开实际修改层级和联动，不重做报表筛选。

A 的平台 `ReportRow` 与 B 的界面 `ReportRow` 位于不同模块，不可互换；输入是平台事实，输出是业务维度行。A 的 MetricDefinition/ReportContract 是唯一指标字典，B 只据此计算比率和兼容桶，不另写一套字段口径。

新查询快照默认有效 15 分钟，材料含有序显示行、指标、趋势、对象引用及版本，保存在数据库。管理预览复制必要事实且有效 5 分钟；提交任务和导出保存自己的冻结副本。导出产物有效 24 小时，读取时重新检查当前权限。

基线 `f8e4eb8` 仅含已确认设计；2026-09-30 静态核实迁移唯一 head 为 `material_push`。新链固定为 `material_push → ads_reporting_data → reporting_queries → ad_management`，文件分别为 `ads_reporting_data.py`、`reporting_queries.py`、`ad_management.py`。实施前根端重新核实 head，若出现其他合法迁移，只调整本轮新迁移的父链，不能覆盖历史。

统一新队列为 `ads-directory`、`ads-reporting`、`ad-management`。A负责前两个独立消费者和共同共享配额；C负责管理消费者。新增开关及默认值写入模板、API/Worker/Beat注入和部署手册，实际启用遵守原有发布流程。本计划不让模板默认 false 变成“已可用”。

## 3. 本地执行和验证前置

- [ ] 读取 `AGENTS.md`、`config/README.md`、`config/environments.json` 和本计划/阶段计划；运行 `git status -sb`、`git rev-parse --show-toplevel`，确认独立仓库且只处理本任务文件。
- [ ] 使用受控测试环境配置 `DATABASE_URL` 与 `TEST_REDIS_URL`；测试数据库名含独立 `test` 段，Redis为与应用不同的非零库。用现有 guard 验证，不输出 DSN或读取私密配置给模型。

```bash
# backend/；只校验当前测试进程的配置，不连接外部平台
uv run --frozen python -c 'import os; from app.core.config import settings; from tests.database import require_test_database, require_test_redis; require_test_database(str(settings.DATABASE_URL)); require_test_redis(os.environ.get("TEST_REDIS_URL", ""), settings.REDIS_URL); print("test environment validated")'
```

- [ ] 确认可用 `uv` 和仓库要求的 Bun。优先现有 Bun 或仓库 `.tools/node_modules/.bin/bun`；可执行文件定位不改变依赖版本，不重新生成锁文件。禁止用 `scripts/test-local.sh`，该脚本含 `docker-compose down -v`，不适合共享工作区验证。
- [ ] A1 纯合同测试使用 `--confcutdir=tests/contracts`；数据库任务沿用根 conftest 的真实迁移/session/context/redis_client。并发任务使用真实多连接/进程或线程屏障，替身只在 TikTok 网络传输边界。
- [ ] 已知全仓失败记录在 `docs/implementation-progress.md` 的 2026-09-29 条目；实施中如遇相关失败，应在原基线确认归属。不要将环境错误计为 RED，也不要宣称未跑或已有失败的全仓通过。

## 4. 执行、提交与集成验收

- [ ] 顺序完成 A1–A8、B1–B6、C1–C7。每个任务先得到目标行为的 RED，再实现并 GREEN。后续测试发现缺口时修正其归属任务；不为文档、菜单文字等低风险变化机械编写镜像测试。
- [ ] 领域实现可以在接口稳定后并行准备，但依赖任务的验证必须使用已经完成的真实合同。A/B/C共享文件与迁移由根端串行合入；不以临时桩接口称阶段通过。
- [ ] 每任务使用 Files 的精确路径 `git add`，提交前检查 `git status -sb`、`git rev-parse --show-toplevel`、`git diff --cached --check` 与暂存差异；采用各任务给定提交主题，同步更新进度。禁止 `git add .`、自动推送或创建合并请求。
- [ ] 阶段 C 完成后，在 backend 执行下列最终专项与静态检查，实际输出需无失败、类型错误及 Alembic 漂移。相关目录并非本轮已经存在的产品实现。

```bash
uv run --frozen pytest tests/modules/ads tests/modules/reporting tests/modules/ad_management tests/integrations/tiktok/test_ads_read_adapters.py tests/integrations/tiktok/test_reporting_adapters.py tests/integrations/tiktok/test_management_adapters.py tests/acceptance/test_reporting_workspace.py tests/jobs/test_ads_reporting_queues.py -q
uv run --frozen ruff check app/modules/ads app/modules/reporting app/modules/ad_management app/integrations/tiktok tests/modules/ads tests/modules/reporting tests/modules/ad_management
uv run --frozen ty check app
uv run --frozen alembic check
```

- [ ] 根目录执行 `bash scripts/generate-client.sh`；前端运行下列命令，确认新测试纳入 workspace project，而不是“未找到测试”。`bun run build` 也生成最新路由树。

```bash
bunx playwright test tests/ads-workspace.spec.ts tests/ads-reporting.spec.ts tests/ad-management.spec.ts tests/workspace-shell.spec.ts tests/build-task-pages.spec.ts --project=workspace --workers=2 --reporter=line
bun run build
```

- [ ] 最终记录 `docs/validation/2026-09-30-ads-reporting-integration.md`（实际实施日变化则用实际日期）：通过数、数据库/Redis隔离、对象数量和查询/采集耗时、版本/覆盖/权限/未知结果行为、API和MCP各自证据级别。达到目标采集周期须有量测，不能根据定时器设为30分钟就认定达标。
- [ ] 真实平台联调分开执行：读接口先确认BC与账户；写接口需具体目标、前后值及影响范围。当前 API 未授权则保持未联调状态；不触发真实测试广告、订阅或账户配置来绕开缺少授权。
- [ ] 若用户要求部署，先完成目标环境的具体变更清单和配置值，按部署手册备份、恢复验证、迁移、进程配置核对及业务验收；测试服不是生产。当前实施计划不包含未经要求的发布操作。

## 5. 设计覆盖自审

| 设计部分 | 承接任务 |
| --- | --- |
| §1–2 全广告范围、复用基础 | A1–A4、C1/C2，共同合同 |
| §3 六维工作台、搜索/保存/导出 | B1–B6、C7 |
| §4 命名、异常、改名迁组 | A1/A4/A5、B2/B3、C5 |
| §5 平台身份和能力边界 | A1/A3/A6、C2/C3 |
| §6 指标、覆盖、历史、增量 | A5–A7、B2–B4 |
| §7 周期、回补、队列和版本 | A4/A6/A7/A8、B4、C5 |
| §8–9 批量影响、恢复、权限、并发 | C1–C7、B3 |
| §10–11 模块、模型、本地 API | A2、B1/B3/B4、C1/C6 |
| §12 双通道与未来推送 | A1/A3/A6/A7、C2；未来事件复用 request_sync，不新增假回调 |
| §13–14 验收、证据与不确定能力 | 各任务GREEN、三份阶段验收及本文件集成验收 |

## 6. 审阅与执行方式

用户审阅本总计划及三份阶段计划后选择一种方式：

- **逐任务分工与复核（推荐）**：按 subagent-driven-development 逐项实施和独立检查，根端负责共享接口、迁移和整合。21项任务涉及真实投放写入、后台恢复及多层数据口径，逐项检查有助于尽早发现跨阶段偏差。
- **主代理连续实施**：按 executing-plans 由主代理执行全部任务，完成后统一独立复核；上下文切换较少，独立检查集中在末尾。

本轮只编写和检查文档。设计确认不代表尚未审阅的实施计划已获执行批准；保留用户随后选定的执行方式，实施时不重复询问。
