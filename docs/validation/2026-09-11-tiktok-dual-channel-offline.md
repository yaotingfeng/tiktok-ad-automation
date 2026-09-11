# TikTok 双通道离线验收

任务基准日期为 2026-09-11；本记录于 2026-09-12 整理。**本地后端第二轮完整矩阵 2203 passed、9 skipped，前端 workspace 348 passed；Linux 与真实服务仍未验，不宣称 23 项全部验收通过或已可生产。** 本地合成服务证明代码行为，不能证明真实客户端注册、授权、服务能力或目标环境配置。

## 版本与最终结果登记

| 项目 | 当前记录 |
| --- | --- |
| 已提交依赖 | P3.3/P3.4/P3.5：fe7a855；P2.3：0485de0；P2.5：b3f9ada；P2.4：2429a22；完整提交归属见实施进度 |
| 本次完整代码提交 | `10da9fda9ee54c615305502f8da10a1ecab8d34a`；第二矩阵前后 767/767 app/tests/config 冻结 SHA256 均一致，提交字节与被测版本一致；后续仅补记本文和实施进度中的提交编号 |
| 目标 Alembic head | mcp_cover_evidence；最终 heads/current 均为唯一 head，check 无差异，三个命令退出 0 |
| 后端首次完整矩阵 | 2149 passed / 36 failed / 9 skipped / 8 errors，1001.76s；退出 1，实际失败与修复经过保留如下 |
| 后端第二轮完整矩阵 | **2203 passed / 9 skipped，1304.32s（21:44），退出 0**；包含本轮 flow 与双基点迁移 |
| 实际本地环境 | macOS 26.3 arm64 / Python 3.14.6 / PostgreSQL 17.5 (Homebrew) / Redis 8.0.1；均为隔离测试资源 |
| 前端 workspace | 本轮 348 passed / 3.9m，退出 0；合成 API 浏览器全组 |
| TypeScript / Vite | 本轮 `run-frontend run build` 退出 0；tsc 通过，Vite 529ms |
| Ruff app/tests 与全 app ty | 本次整合候选均通过；矩阵前后源码未变化 |
| 独立迁移数据库与历史证据 | 两个隔离数据库升级用例及相关历史证据回归已纳入第二轮通过矩阵 |
| Linux prefork | 未执行；当前 macOS 不支持对应真实终止验收 |
| 真实 MCP/OAuth/TikTok/R2/版权方及生产发布 | 未执行 |

阶段完成、审查及提交清单以 [实施进度](../implementation-progress.md) 为准。开发过程详细命令日志位于本地受控任务报告目录，不属于生产验收或可发布凭据。

## 已执行的跨阶段完整合成链路

`backend/tests/integrations/tiktok/test_dual_channel_flow.py` 与 `flow_inputs.py` 使用实际 gateway、独占 PostgreSQL 测试库、真实 Redis、SDK HTTP/MCP Streamable HTTP 边界替身。测试不替换授权、claim、任务锁、生产准备函数或 gateway 结果。

最后定向命令（仓库根，本地 runner 注入专用测试环境）：

```bash
.superpowers/sdd/2026-09-11-tiktok-mcp-implementation/run-test -m pytest tests/integrations/tiktok/test_dual_channel_flow.py -q -x --tb=short
```

结果：**2 passed in 28.02s**，覆盖 OFFICIAL_API 与 OFFICIAL_MCP；两新文件 Ruff check/format 通过。该定向结果发生在最终封面证据迁移之前；两用例现已纳入最终 head 的第二轮完整矩阵并通过。不是 Linux prefork 实测。

两通道分别经过真实场景任务、ffmpeg/ffprobe 原件验证、生产分片入库、源 URL 上传与强回读、跨账户目标 VID/MID、版权方 GET 取已有链接、能力刷新、草稿准备/预览、提交、独立 CTA、目标封面准备与读取、三级 ENABLE 创建及独立父子/金额/素材回读，最终任务投影 COMPLETED。MCP 用例 API App 配置为空。

- 目标封面仍 PENDING 时实际提交可准备的预览；Campaign 未 arm、三级 ID 为空且零新增业务 HTTP。封面强核实后才允许广告创建。
- 源和目标实际返回不同 VID/MID，保存各自账户/route；原件用途只在完整强回读后释放，成功来源重复投递零 HTTP。
- 远端读取来自传输边界独立存储的实际已发送内容及远端 ID，不从本地 request_body 填充响应；金额字面断言 100.25/1.08，保留 Smart+ ID、目标素材与父级。
- CTA 详情故意缺 advertiser_id，确认保留已知 portfolio ID 及 INCOMPLETE，未用请求账户补全。
- 注册、主体、权限、工具 schema、视频五字段 policy 全部明确 **SYNTHETIC**；对象存储为字节级替身，版权方替身只接受指定 synthetic host 的两个 GET 路径，未访问真实服务。

## 能力与证据矩阵

| 能力/边界 | 精确离线测试入口（backend/tests 下） | 最终状态 |
| --- | --- | --- |
| DTO、严格 JSON、SDK/MCP 序列化、Decimal/大 ID | contracts；integrations/tiktok | 第二轮完整矩阵通过 |
| 当前 actor/tenant/BC/conn 与完整目录发布 | modules/accounts/test_capability_boundaries.py；test_runtime_directory.py；test_runtime_directory_mcp.py | 第二轮完整矩阵通过 |
| 正常 token 轮换、刷新 pending/unknown、outbox 竞争 | modules/accounts/test_mcp_refresh.py；test_mcp_refresh_concurrency.py | 第二轮完整矩阵通过 |
| 观察更新不废任务，权限变更仍围栏 | modules/accounts/test_directory_observation_migration.py | 第二轮完整矩阵通过 |
| 场景握手后撤权/失 claim、刷新终态 | modules/builds/scene/test_dual_channel_scenes.py | 第二轮完整矩阵通过 |
| 视频/图片实际远端事实与只读门禁 | modules/accounts/test_material_gateway.py；test_material_channel_policy.py | 第二轮完整矩阵通过 |
| URL 上传一次、UNKNOWN 保留用途、晚回执/清理竞争 | modules/materials/test_material_upload_worker.py；test_unknown_original_fences.py | 第二轮完整矩阵通过 |
| 原 route 跨账户目标与封面 | modules/materials/test_frozen_material_routes.py；test_channel_covers.py | 第二轮完整矩阵通过 |
| 创建公平准入、NOT_SENT 原 attempt、UNKNOWN 不再发 | modules/builds/test_gateway_fairness.py；test_channel_execution.py | 第二轮完整矩阵通过 |
| 原授权只读/新授权独立审计 | modules/builds/test_reconciliation.py；test_historical_read.py | 第二轮完整矩阵通过 |
| 最终 head 跨阶段升级 | integrations/tiktok/test_dual_channel_migration.py（旧 build head / P2 material head 各升最终 head） | 两用例均在第二轮完整矩阵通过 |
| 冻结历史与 NULL 不猜今日默认 | modules/builds/test_route_migration.py；scene/test_scene_route_migration.py；modules/materials/test_material_route_history.py | 第二轮完整矩阵通过 |
| Linux 接收后 hard kill、原通道读回且 create 总数 1 | modules/builds/test_prefork_deadline.py；test_prefork_read_recovery.py；modules/materials/test_source_upload_prefork.py；test_validation_prefork.py | 本机未执行，必须独立验收 |
| 草稿连接选择/清除、冻结投影、权限、核查回执恢复 | frontend/tests/build-channels.spec.ts；build-preparation.spec.ts；build-preview.spec.ts；build-task-pages.spec.ts；tenants-accounts.spec.ts | 本轮 workspace 全组 348 passed / 3.9m |

最终未通过或跳过项目必须逐项解释，不能用总 passed 掩盖。旧 API 与 MCP 都需覆盖；通道数量不是实际服务授权证明。

## 最终执行命令与迁移规则

仅在 root 通知完整集成 READY 后运行后端，服务配置指向专用测试 PG/Redis，绝不共享生产数据。仓库 CI 声明 Ubuntu、PostgreSQL 18、Python 3.14、Bun 1.4.2；本轮没有在该 CI 环境运行，实际本地版本见上表。以下命令从 backend 执行：

```bash
uv run pytest tests/contracts tests/integrations/tiktok tests/modules/accounts tests/modules/materials tests/modules/builds tests/jobs -q --tb=short -rs
uv run ruff check app tests
uv run ty check app
uv run alembic heads
uv run alembic current
uv run alembic check
```

本地已安装工具可使用等价直接 binary；ty 必须指向仓库虚拟环境（backend 下 `../.venv/bin/ty check --python ../.venv app`），不得误用系统解释器。结果按实际命令登记，mypy 只是额外证据。

前端从根执行 `bun run --filter frontend build`，在 frontend 执行 `bun x playwright test --project=workspace`。本轮实际使用本地 Bun wrapper：`PLAYWRIGHT_BASE_URL=http://127.0.0.1:5174 .../run-frontend x playwright test --project=workspace --workers=2 --reporter=line`（348 passed / 3.9m，退出 0），以及 `.../run-frontend run build`（tsc 通过，Vite 529ms，退出 0）；wrapper 工作目录为 frontend。workspace 使用合成 API 响应，不执行真实登录/授权；真实 API 浏览器集成和 Linux prefork 是另行验收项。

`test_dual_channel_migration.py` 只在新建隔离数据库从 `mcp_directory_bc_scope`、`mcp_material_routes` 各升级一次，并比较旧请求正文/摘要、ID、原件和已知回执逐值不变。历史无法证明 route/auth 的记录保持 NULL/阻断，不补今日默认；新增 route 不可改，非空证据拒绝降级。封面 mcp_cover_evidence 需验证原视频摘要冻结、真实 receipt_facts 保留及历史 NULL 不回填。最终 heads 必须唯一；禁止 downgrade 共享测试库来凑历史测试。

## 未验证与发布条件

真实 MCP 注册和 PKCE 闭环、实际主体/scope/BC 权限、实际工具与响应覆盖、视频五字段服务 policy、上游刷新/写入重放保证均未获得本轮真实证据。MCP 视频门禁继续关闭；Linux prefork 未跑，目标环境出站/资源/恢复能力未验。详细逐项模板见 [真实 MCP 联调](../acceptance/live-mcp.md)。

发布必须另获具体目标环境授权并完成 [发布手册](../runbooks/deployment.md)；骏伯按 [生产手册](../runbooks/production-junbo.md)。本记录不执行部署，不改变当前生产 SHA 或自动化开关。


## 首次完整矩阵与修复记录

第一轮相同范围矩阵自然执行完成，实际为 **2149 passed、36 failed、9 skipped、8 errors / 1001.76s**，退出 1。不是全绿。初分组为：MCP期限异常分类1项、旧目录观察语义断言1项、旧schema播种3项、缺BC默认的旧准备/展开fixture（8 failed + 8 errors）、封面强证据与下游执行链20项、素材旧review入口3项。后续已按完整堆栈定位并修复，未放宽授权/route/MD5/回读规则。修复后结果另记，不改写这次失败计数。

本次两条完整flow与两个最终head迁移用例均通过，但不能代替全矩阵结论。9个跳过中，8个是Linux-only进程验证（广告harddeadline1、双通道readrecovery2、sourceupload双通道×两种进程场景4、validator1），1个是MCP专用文本envelope用例的API参数不适用。Linux八项仍待真实执行。


## 首跑后的定向修复验证

- 目录观察旧断言与三处历史schema播种按当前合同修正；MCP期限路径修复 RemoteCallError 被其 DomainError 基类分支降型而丢失 effect 的问题。相关联合 **75 passed / 30.22s**。
- 素材/封面组修复已有核实 image 且无新 job 时被新 MD5 检查提前阻断的回归，保留新 job/历史 job 的原摘要、route和回执围栏；迁移旧测试边界并保留业务断言。相关联合 **187 passed / 209.91s**。
- 缺默认连接组补真实合成 BC binding/default/授权与发现事实；后续真实预览诊断发现旧 URL 准入键落入短 base，迁为实际两个 upload operation。准备/展开与最小真实 preparation/freeze 联合 **23 passed / 336.16s**。没有放宽生产默认选择、上传期限或提交门禁。

以上为独立定向结果，不相加作为完整验收。第二轮完整矩阵从同一冻结候选执行，增加 `-rs` 保存具体跳过原因；自然结束为 **2203 passed、9 skipped / 1304.32s**，退出 0，零 failed/error。前端源未再变化，不重复以 UI 运行替代后端验证。

## 最终跳过清单与证据边界

| 路径（backend/tests 下） | 数量 | 实际原因 |
| --- | --- | --- |
| integrations/tiktok/test_material_adapters.py:391 | 1 | MCP 专用文本 envelope，API 参数不适用 |
| modules/materials/test_source_upload_prefork.py:74 | 4 | 双通道真实多 worker / hard-kill 需 Linux |
| modules/materials/test_validation_prefork.py:247 | 1 | 验证 worker 的真实 prefork hard-kill 需 Linux |
| modules/builds/test_prefork_deadline.py:30 | 1 | 广告 prefork 硬期限需 Linux |
| modules/builds/test_prefork_read_recovery.py:46 | 2 | 双通道真实 hard-kill 后只读恢复需 Linux |

这 8 个 Linux 用例没有执行，不能以合成 HTTP 的单发送/恢复通过代替进程终止证据。实际 OAuth/MCP 注册、真实主体/scope/工具、视频五字段服务策略及 R2/版权方/广告写入同样未执行。部署前还需按发布版将旧 SDK URL 准入覆盖迁为逻辑 operation，尤其两个视频 upload lease 必须严格大于 905000ms；独立 `auth_refresh` 键保持原名，测试额度不是生产默认。配置迁移细节见 [发布手册](../runbooks/deployment.md#准入配置键迁移)。
