# 2026-09-29 外部素材批量推送：本地验证

## 交付范围

本地 FastAPI 接收/查询、HMAC 验签、PG 批次和外部素材修订、只读外部 URL 校验、Celery 恢复、默认 BC 管理 UI、生成客户端、迁移与[接入文档](../integrations/material-push.md)。基线提交为 `3b841cb`（原名上传修改）；没有推送、部署、真实素材下载或 TikTok 写入。

新 worker 接回现有 URL 上传/回执/封面/分发机制，不新增 TikTok HTTP 网关。来源 HTTP 和平台请求均在传输边界使用合成替身。PostgreSQL/Redis 为真实本地独立测试资源。

## 已通过的验证

- `tests/modules/materials/test_push.py`：42 项。验签时效/篡改/路径/方法、租户范围、原子批次登记、同请求串行幂等、3 字段与原名、默认 BC/旧批次冻结、逐项失败、同内容复用、链接续期、未知上传不重发、历史版本/目录过滤、双 worker 领取、租约修复/扫描饥饿、SSRF/固定 DNS IP/无跳转、大小/摘要/视频容器、实际 SDK URL 上传、外部原件不可清理/修改及预览。
- 素材/租户相关回归组：295 项，107.95 秒；该组包含当时 40 项 push 用例。涵盖 cleanup/abandonment/reconcile/scan、tenant library/read/matching、URL/source uploads、validation dispatch/retry bounds、ingest API/models、part receipts、MCP URL upload policy、original uses、租户权限与并发、push 迁移。后来补充的 2 项 push 防护用例另跑通过，不把重叠批次相加为测试总数。
- 最终补充组：`test_read_apis.py test_push.py test_push_migration.py tests/modules/tenants` 共 94 项，7.92 秒。旧 S3 上传读取用例须显式 `OBJECT_STORAGE_PROVIDER=s3`；本地日常环境为 R2 时原路由按既有规则返回 `ingest_api_required`，不是新增接口故障。
- 最后独立执行 `test_push_migration.py test_material_upload_adapters.py test_url_sdk_contract.py`：132 项，15.71 秒；包括实际 SDK/MCP 适配器的离线传输边界及新增的“已有批次拒绝降级”检查。
- 前端 `tests/tenants-accounts.spec.ts --project=workspace --workers=2`：108 项，1.9 分钟；包括选择/清除默认 BC、首次进入使用默认值和空默认取第一条。最初新增导航测试误用非数字 BC ID，被已有路由校验拒绝；改用合成数字 BC ID 后通过，未放宽产品路由。
- 生成 OpenAPI 客户端、TypeScript/Vite build、Ruff、ty、`git diff --check`；Celery 显式导入后核对两个新增任务已注册。新 UI 沿用现有 shadcn Field/DirectoryPicker，不引入新的组件包或样式体系。
- `alembic check` 无模型迁移差异；独立历史测试库从 `audience_targeting` 升级，验证原租户信息保留、跨租户默认 BC 外键拒绝、重名拒绝而不改数据、安全空库降级/重升、有默认 BC 或推送批次拒绝破坏性降级。

复现环境前缀（仅为本地合成测试资源，不是部署配置）：

```bash
DATABASE_URL=postgresql+psycopg://yaotingfeng@localhost:5432/tkada_material_push_20260929_test \
TEST_REDIS_URL=redis://localhost:6379/14 OBJECT_STORAGE_PROVIDER=s3 \
uv run --frozen pytest tests/modules/materials/test_push.py \
  tests/modules/materials/test_push_migration.py tests/modules/tenants \
  tests/modules/materials/test_read_apis.py -q
```

从 `backend/` 运行。迁移测试自动创建/清理其随机命名的专用历史数据库，不对现有业务库执行 downgrade。

## 扩展回归的既有失败

全量素材/租户目录探索并非全绿：在 82 项通过后遇到 `test_bc_seeding_concurrency.py::test_parallel_aliases_seed_once_then_share_three_targets_and_reuse_for_fourth`，期望 ready、实际 blocked。另运行较早的 `test_primary_account_migration.py`，其当前 ORM 写入历史 schema 不存在的素材操作列而失败。

为区分新回归，使用 `git archive HEAD` 将无本轮修改的 `3b841cb` 导出到 `/tmp/tkada-push-baseline.yUeLmB`，用独立 `tkada_material_push_baseline_20260929_test` 库与 Redis DB 13、全合成配置运行两个失败文件及旧 read API：**同样 2 failed、9 passed**。两项失败均已存在于基线，没有在本任务中扩大范围修改。不能把本记录表述为全仓库所有测试通过。

本轮新增默认 BC 列会影响共享历史租户夹具，已将该测试夹具改为只插入自最初版本存在的租户列，避免以今日 ORM 字段播种旧租户表；没有改写历史 Alembic 迁移。

## 发布前仍需完成

管理员提供并配置真实接入 key/secret、执行成员、允许租户和 R2 主机；按部署手册备份、迁移、重载 API/全部 Worker/Beat，核对入库开关、ffprobe/临时磁盘、默认连接与账户和调用额度。最后在明确获批的租户/BC/素材范围真实推送并回读 VID、原名和库内状态。本地通过不代表已经上线或真实联调通过。
