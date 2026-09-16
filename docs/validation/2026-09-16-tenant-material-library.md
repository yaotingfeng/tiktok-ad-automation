# 租户统一素材库与 BC 首次转存验收

日期：2026-09-16。范围：本地实现、独立 PostgreSQL/Redis 和 HTTP 传输边界测试。未推送、未发布测试或生产环境，未调用真实 TikTok 写接口。

## 交付行为与入口

- 素材库按租户显示，切顶栏 BC 保留列表、搜索及页码。素材详情显示实际 BC/账户副本；新上传使用当前 BC，进行中和刷新恢复中的请求固定原目标。
- 搭建自动匹配、手动选材覆盖租户内容；可信摘要一致的重复上传去重，历史文件名仍可匹配。自动匹配续页更换代表时不会重复入组。
- B 内已有目标副本则核实复用；已有合法来源则原生共享；没有来源时仅向 B 主素材账户 URL 转存一次，然后向 B1/B2/B3 共享。后续 B4 复用来源。
- 持久 seed 按租户、目标 BC、可信内容唯一，消费者各自持有目标分发身份。未知结果、撤权、绑定代数变化不会导致换账户或重复上传。
- 视频和封面分别核实真实目标标识。同内容别名复用真实 SOURCE 证据；主账户自身只读核实已有图片，不能向自己共享或伪造上传历史。

核心入口：

| 责任 | 代码 |
| --- | --- |
| 租户目录、候选、分页去重 | `materials/router.py`、`catalog.py`、`repository.py` |
| 内容身份、自动/手动选材 | `materials/content_identity.py`、`builds/drafts.py` |
| 提交后的目标准备 | `materials/distribution.py::ensure_target_asset` |
| 单 BC 首次转存及等待恢复 | `materials/bc_seeding.py::ensure_bc_seed`、`resume_seed_dependency` |
| 只读预览与主账户检查 | `materials/readiness.py`、`source_selection.py` |
| 真实 SOURCE/IMAGE、同内容别名 | `materials/cover_sharing.py`、`covers.py`、`source_cover_service.py` |
| 统一目录与上传目标冻结 | `frontend/src/features/materials/MaterialsPage.tsx` |

上述后端路径均相对 `backend/app/modules/`。用户无需新增选择素材来源 BC 或点击同步按钮；正常搭建提交触发准备。

## 数据迁移

迁移链：`material_share_receipts → tenant_material_library → material_bc_seed → material_target_scope`。

- 消费引用改为 `(tenant_id, material_id)`；原件、上传尝试及入库记录保留 `(tenant_id, bc_id, material_id)`。
- 真实目标映射及 pending 唯一键包含 BC；操作、分发独立引用该租户已登记的 BC。
- seed 与真实主账户分发通过有范围的延迟外键关联，初始为空，旧分发 `seed_id` 为空；不回填历史上传事实。
- 实际从旧 head 升级并核对原记录；空 seed 可降级，已有 seed 或跨 BC 消费时拒绝不兼容降级。
- 最终专用测试库 `alembic upgrade head` 和 `alembic check` 通过，无模型差异。迁移仅在本地测试库执行。

## 行为验证

各组存在重叠，不累计宣称为全仓测试总数。

| 测试组 | 最后有效结果 | 覆盖 |
| --- | --- | --- |
| 内容/迁移/草稿/预览/素材执行 | 46 passed | 跨租户拒绝、原上传约束、旧 head 迁移、重复内容与续页 |
| 目录/匹配/导入/重试七组 | 81 passed | 租户读取、实际来源、历史批次与写入身份 |
| 账户素材网关 | 22 passed | API/MCP 真实授权来源读取，零分发 outbox |
| 分发、源上传、批次与 seed 广泛回归 | 191 passed | 新旧执行、并发、UNKNOWN、权限与冻结身份 |
| seed 最终相关回归 | 28 passed | 包含新增11个 seed + 1个真实 PG/Redis 并发用例 |
| 跨 BC 与别名封面专项 | 20 passed | SOURCE/IMAGE、真实身份、主账户自身、内容变化、UNKNOWN |
| 最终封面既有五组回归 | 176 passed | throughput、fast path、bulk verification、channel covers、source events |
| 双通道同 BC 完整集成 | 2 passed | API/MCP 上传→视频/图片共享→预览/提交→创建参数→独立回读 |
| 双通道跨 BC 完整集成 | 2 passed | 真正 A 入库，B primary 单次 URL 转存，后续 B 共享，原来源不变 |
| 前端素材/导入/上传基础/搭建四组 | 145 passed | 切 BC、历史队列、选材、权限、页面流程 |
| 最终恢复待确认上传专项 | 5 passed | 含新增第146个场景：刷新恢复请求时切 BC 保持原管理器 |

跨 BC 完整集成使用两个独立 BC 和账户，实际调用服务代码完成 A 原始上传，再提交 B 搭建；并未修改素材或入库批次的原始 BC 来模拟跨 BC。HTTP 请求边界断言 URL 上传次数、目的账户、原生共享范围、最终 VID/image_id、广告创建参数和独立回读。

并发测试使用同内容不同 material_id，同时准备 B 内三个目标，断言只有一次到 primary 的 URL 上传，随后逐目标原生共享，第四账户不再转存。已发送 UNKNOWN 保留原身份，不重复发起。

## 复审与静态验证

- 独立复审发现并关闭：同内容别名无法复用 SOURCE 封面；选材部分勾选后全选误取消已有项。均补充回归。
- 根代理另补目标 BC 外键和自动匹配续页代表变化去重，先复现失败再修复；上传恢复目标冻结同样先 RED 后 GREEN。
- 最终独立复审未发现新的可确定 P1/P2。审查包含冻结授权、内容身份、来源所有权、迁移、选材及上传生命周期；复审代理执行静态/导入/diff 检查，行为通过结果来自各实施测试。
- 44 个变更 Python 文件 Ruff/格式检查通过，26 个生产模块 mypy 通过。Alembic env 的既有 E402 延后导入仅在该文件检查时排除，不修改历史导入结构；新增注册导入自带 E402/F401 标注。
- OpenAPI 客户端已重新生成，变化仅为读取范围及来源/内容字段；最终 TypeScript/Vite 构建与修改前端文件 Biome 通过。

## 可复现命令与边界

先按 `config/README.md` 定位本地环境；给各执行器分配不同的 `_test` PostgreSQL 库和非零测试 Redis DB。测试使用 `OBJECT_STORAGE_PROVIDER=s3`、`S3_REGION=us-east-1` 和传输替身，不需要真实素材或平台凭据。

```bash
# backend/；DATABASE_URL 和 TEST_REDIS_URL 指向专用本地测试实例
uv run --frozen alembic upgrade head
uv run --frozen alembic check
uv run --frozen pytest tests/modules/materials/test_content_identity.py tests/modules/materials/test_tenant_library_migration.py tests/modules/builds/test_tenant_library_builds.py tests/modules/builds/test_drafts.py tests/modules/builds/test_draft_catalog.py tests/modules/builds/test_previews.py tests/modules/builds/test_material_execution.py -q
uv run --frozen pytest tests/modules/materials/test_bc_seeding.py tests/modules/materials/test_bc_seeding_concurrency.py -q
uv run --frozen pytest tests/modules/materials/test_tenant_library_covers.py -q
uv run --frozen pytest tests/integrations/tiktok/test_dual_channel_flow.py -q

# frontend/
bunx playwright test --project=workspace tests/materials.spec.ts tests/r2-ingest.spec.ts tests/upload-foundation.spec.ts tests/build-preparation.spec.ts --reporter=line
bun run build
```

本地模拟覆盖不等于服务器发布或真实 TikTok 联调。以后发布需前后端/Worker 同版，按现有发布手册排空、备份并独立恢复验证后迁移；无新功能开关。真实平台仍需在用户明确授权的 BC/账户与具体搭建范围内验收。

设计/计划先行提交：`76ca073 materials: document tenant library and BC seeding plan`。实现聚焦提交主题：`materials: unify tenant library and seed each BC once`。
