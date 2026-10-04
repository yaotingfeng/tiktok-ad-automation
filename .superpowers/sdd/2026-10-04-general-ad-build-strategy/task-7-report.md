# Task 7 report — end-to-end regression and validation

基线：`637dda9`

## 改动

- `backend/tests/modules/builds/test_execution_assets.py`
  - 保留四套 DB-free supplemental 结构夹具，断言规划出的广告数量、广告级冻结素材集合、系列/组预算层级和最高价值字段。
- `backend/tests/modules/builds/test_previews.py`
  - 增加真实 session 的 A/B draft/preview 隔离回归，以及四种策略结构矩阵对 `PlannedAd`、`PreviewAdMaterial`、CTA、cover、预算和 bid 冻结字段的断言。
- `backend/tests/modules/builds/test_material_execution.py`
  - 增加真实 `plan_material_slice`/`ensure_target_assets` 账户素材去重回归，并从冻结广告读取各自素材集合。
- `backend/tests/modules/builds/test_partial_material_execution.py`
  - 增加真实不可用素材、UNKNOWN 广告恢复后冻结行不变的回归；既有 `test_cover_execution.py` 覆盖 pending cover、部分广告失败和 `prepare_request` 封面 fence。
- `backend/tests/modules/builds/test_cover_execution.py`
  - pending cover 的 RETRY/RECONCILE 回归现在同时比较恢复前后的 `PreviewAdMaterial` 快照，确认封面恢复不改写冻结素材。
- `frontend/tests/build-preparation.spec.ts`
  - 预览夹具补充组预算/最高价值冻结摘要断言。
- `frontend/tests/build-task-pages.spec.ts`
  - 未知素材核查结果断言只读原冻结任务，不重新请求广告规划。
- `docs/implementation-progress.md`
  - 追加 Task 7 进度、命令、迁移 head、环境阻塞和无真实写入说明。
- `docs/validation/2026-10-04-general-ad-build-strategy.md`
  - 区分离线证据、无 staging 证据和未授权的真实平台写入，记录精确 blocker。

## 验证

通过：

- 7 项 harness 仅是 DB-free supplemental checks：`uv run pytest --confcutdir=/tmp /tmp/task7_offline.py -q` → `7 passed`。该临时测试文件由提交中的 `test_execution_assets.py` Task 7 夹具抽取，仅绕过仓库 session migration fixture；不连接数据库，不能替代新增的 DB-bound tests。
- 前端聚焦回归：`PATH="$PWD/.tools/node_modules/.bin:$PATH" bun run --cwd frontend test -- tests/build-preparation.spec.ts -g '预览展示冻结定向|当前版本已有预览' --workers=1` → `2 passed`。
- `uv run ruff check backend/tests/modules/builds/test_execution_assets.py` → passed。
- `uv run python -m compileall -q backend/tests/modules/builds/test_execution_assets.py` → passed。
- 修改的四个 DB-bound 测试文件（`test_previews.py`、`test_material_execution.py`、`test_partial_material_execution.py`、`test_cover_execution.py`）`ruff` 与 `compileall` 均通过。
- `git diff --check` → passed。
- `uv run alembic heads` → `20261005_preview_ad_material_strategy` 与 `provider_kinds_expansion` 两个 head 可解析。

阻塞/未执行：

- 完整 backend pytest、Alembic upgrade/downgrade、PostgreSQL/Redis 并发和恢复验证：专用 PostgreSQL `127.0.0.1:15432` 拒绝连接，Redis 也未启动。
- DB-bound smoke：`DATABASE_URL=postgresql+psycopg://user:pass@127.0.0.1:15432/tk_ada_test uv run pytest tests/modules/builds/test_previews.py -q -k 'two_drafts_freeze or strategy_structure_matrix'` 在 Alembic session fixture 连接阶段得到 5 errors（connection refused），所以没有把这些测试误报为通过。
- 完整前端回归是手动中断的部分结果：`44 passed / 13 failed / 1 interrupted / 60 not run`；失败来自既有策略选择器/草稿 fixture，聚焦受影响场景通过，未宣称全量通过。
- staging 预览、TikTok/MCP 读回、真实广告创建/启用写入：本轮均未执行，也没有生产验收结论。

提交前将只暂存本 Task 7 文件，保留并发 reporting/ads 工作区及其他无关改动。
