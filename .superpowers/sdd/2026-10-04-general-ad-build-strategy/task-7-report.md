# Task 7 report — end-to-end regression and validation

基线：`637dda9`

## 改动

- `backend/tests/modules/builds/test_execution_assets.py`
  - 增加四套离线结构夹具，断言规划出的广告数量、广告级冻结素材集合、系列/组预算层级和最高价值字段。
  - 增加两个草稿复用同一策略版本时 preview 冻结行隔离、共享素材准备键去重但广告保留独立 `creative_list` 的回归。
  - 增加目标素材不可用、待核实封面、最高价值能力字段缺失和恢复不得重新规划/扩大素材集合的回归。
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

- 离线 Task 7 夹具 7 项：`uv run pytest --confcutdir=/tmp /tmp/task7_offline.py -q` → `7 passed`。该临时测试文件由提交中的 `test_execution_assets.py` Task 7 夹具抽取，仅绕过仓库 session migration fixture；不连接数据库。
- 前端聚焦回归：`PATH="$PWD/.tools/node_modules/.bin:$PATH" bun run --cwd frontend test -- tests/build-preparation.spec.ts -g '预览展示冻结定向|当前版本已有预览' --workers=1` → `2 passed`。
- `uv run ruff check backend/tests/modules/builds/test_execution_assets.py` → passed。
- `uv run python -m compileall -q backend/tests/modules/builds/test_execution_assets.py` → passed。
- `git diff --check` → passed。
- `uv run alembic heads` → `20261005_preview_ad_material_strategy` 与 `provider_kinds_expansion` 两个 head 可解析。

阻塞/未执行：

- 完整 backend pytest、Alembic upgrade/downgrade、PostgreSQL/Redis 并发和恢复验证：专用 PostgreSQL `127.0.0.1:15432` 拒绝连接，Redis 也未启动。
- 完整前端两文件回归在 118 项中先遇到 4 个既有策略选择器/草稿 fixture 失败并中断；聚焦受影响场景通过，未宣称全量通过。
- staging 预览、TikTok/MCP 读回、真实广告创建/启用写入：本轮均未执行，也没有生产验收结论。

提交前将只暂存本 Task 7 文件，保留并发 reporting/ads 工作区及其他无关改动。
