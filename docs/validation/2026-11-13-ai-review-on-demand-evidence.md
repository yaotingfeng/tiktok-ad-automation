# 复盘按需证据读取本地验收

实现及取舍见 [设计](../superpowers/specs/2026-11-13-ai-review-on-demand-evidence.md)。范围为后端任务冻结/读取、DSH 插件工具及对应测试，前端无源代码修改。未做生产部署、真实广告或平台写入。

## 验证环境与命令

真实本机 PostgreSQL 的独立测试数据库 `tkada_review_reading_final_test`，真实独立 Redis 端口 16479（应用 DB 0、锁/并发测试 DB 5）；使用测试专用环境值、APP_ENV_FILE=/dev/null，不加载业务凭据。新增迁移从空库升级且 SQLModel 元数据比较通过。DSH 使用已有 `.runtime/dsh-session-test` 中的官方运行包，Node 24 与镜像基线一致；系统 Node 23 不支持既有入口的 import.meta.main，不作为宿主验收运行时。

后端聚焦套件：`backend/tests/modules/ai_workbench` 全部，另含 reporting 的 `test_dimension_query.py`、`test_snapshot_members_consumers.py`、`test_query_reuse.py`、`test_inline_snapshot_evidence.py`、`test_compact_snapshot_evidence.py`、`test_creative_query_review_fixes.py`、`test_api.py`。用 `-k 'not test_inline_migration_roundtrip_preserves_frozen_versions and not test_engine_api_cursor_and_selection_cross_request'` 明确排除下述基线失败。

新增行为覆盖：61 行 Campaign 报表分页（超过原 50 行）、重复页幂等、后续发布不改变旧页、D7 七个成熟日和金额保持、cutoff 后发布拒绝、索引不授予引用权、建议/提案未读引用拒绝、已读页成功完成、完成后不可再读、跨任务/租户 ID 和 JWT 拒绝、账户范围不可扩张、撤权/版本变化拒绝、摘要篡改/非法页拒绝、读取规则后才能回测，以及未读规则源的清理保护。插件验证任务令牌路由、受限原生注册、分页参数、取消后不调用传输。

## 非绿项与范围

首次扩展 ai_workbench + reporting 大套件在 304 项通过、1 项跳过时中止；3 项失败：大规模快照种子遇到 PostgreSQL `out of shared memory`，跨请求 API 返回 `query_build_busy`，背景队列公平性断言失败。该次并非全套通过，也未修复背景调度或调整共享数据库配置。

聚焦套件识别两个报表原有失败：

- `test_inline_migration_roundtrip_preserves_frozen_versions`：既有迁移往返测试删除不存在的 `ix_material_post_link_post`。
- `test_engine_api_cursor_and_selection_cross_request`：跨请求查询返回 503 `query_build_busy`。

已将修改前 HEAD 的 backend 独立归档到临时目录，在独立测试数据库复跑上述两个用例，结果 **2 failed**，原因相同；只附当前新迁移以便识别数据库 head，未带本轮实现。故从最终聚焦回归中显式排除，不能计为通过。

## 最终结果

- 最终聚焦回归：`220 passed, 2 deselected, 11 warnings`；排除项即上文两项 HEAD 复现失败。
- 新增按需读取/知识/边界测试：`14 passed`。
- DSH 插件 Node 24 原生测试：`9 passed`；部署宿主测试：`4 passed`。
- Ruff 检查与格式、`git diff --check` 通过；前端未修改产品代码，已有构建检查通过。
- 日志仅保留本机临时目录，不提交真实报告、凭据、会话或数据库。
