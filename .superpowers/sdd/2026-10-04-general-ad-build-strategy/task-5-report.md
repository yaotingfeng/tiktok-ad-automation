# Task 5 report — preview API and business summaries

基线：`eeac2f8`

## 完成内容

- 扩展 `PreviewSummary`、`PreviewUnit`、剧目汇总和提交 DTO，冻结并返回预算层级、竞价策略、组/广告生成模式、创意数量、可读结构摘要，以及去重素材数与素材分配引用数。
- 预览和剧目统计使用冻结的素材分配表；组预算按广告组数量计算，系列预算按系列数量计算，并返回对应的“组预算合计”或“系列预算合计”标签。
- 提交详情与提交列表保留预算/竞价策略，提交金额按实际提交层级计算；账户组合展示广告、素材分配和结构摘要。
- 前端预览、输入策略摘要、生成进度和提交明细改用业务语言；新增 `invalid_material_allocation`、`adgroup_budget_unverified`、`bid_strategy_invalid` 调整提示。
- 更新边界夹具和预览页面回归，执行 `scripts/generate-client.sh` 生成并提交 `frontend/src/client/types.gen.ts`，未手改生成文件。

## 验证

通过：

- `uv run ruff check app/modules/builds/preview_schemas.py app/modules/builds/preview_catalog.py app/modules/builds/previews.py app/modules/builds/execution_schemas.py app/modules/builds/submissions.py app/modules/builds/submission_catalog.py`
- `python3 -m py_compile`（上述后端文件）
- `uv run --frozen python -c 'from app.main import app; app.openapi()'`，确认预览/提交 schema 字段已进入 OpenAPI
- `PATH="$PWD/.tools/node_modules/.bin:$PATH" ./.tools/node_modules/.bin/bun run --cwd frontend test -- tests/build-preview.spec.ts -g '预览摘要说明预算层级' --workers=1`（1 passed）
- `PATH="$PWD/.tools/node_modules/.bin:$PATH" ./.tools/node_modules/.bin/bun run --cwd frontend test -- tests/build-preview.spec.ts -g '预览预算将 Decimal 0E-12' --workers=1`（1 passed）
- Biome 检查 Task 5 前端文件（通过）。

受限：

- `uv run pytest tests/modules/builds/test_preview_workspace_api.py -q` 在测试收集前阻断：环境未配置专用 PostgreSQL `DATABASE_URL`。
- `bun run --cwd frontend build` 的失败来自尚未实施的 Task 6 策略页面仍读取旧 `group_size` 字段；Task 5 修改文件未产生 TypeScript 错误。
- 完整 `tests/build-preview.spec.ts` 已通过主要预览场景；三步输入页面验收受现有 `ProviderLinkConfigFields` fixture 缺少能力字段影响，未调用真实 TikTok 接口。

## Fix round 1

- 素材分配次数改为优先统计冻结的 `PreviewAdMaterial` 广告级引用；共享素材按广告映射次数计数，去重素材数仍按素材 ID 去重。
- 对没有广告级映射的历史预览，摘要、剧目、账户组合和提交目录统一回退到冻结组素材与 `PlannedAd` 关系；只读既有冻结数据，不重新规划。
- 冻结详情使用预算层级和竞价策略业务文案；最高价值不展示目标 ROAS，目标 ROAS 仅在策略为 `TARGET_ROAS` 且有值时展示。摘要移除重复的预算/结构文案。

Fix round 1 验证：

- 通过 `uv run ruff check`、`python3 -m py_compile`、Biome（4 个变更前端文件）和 OpenAPI 生成检查。
- 通过前端聚焦回归：`预览摘要说明预算层级、竞价策略与素材分配口径`、`冻结预算保持长Decimal有效位并只移除小数尾零`（2 passed）。
- 后端 `test_preview_workspace_api.py` 未能收集：当前环境未配置测试所需的 `SECRET_KEY`、`PROJECT_NAME`、`DATABASE_URL`、`FIRST_SUPERUSER`、`FIRST_SUPERUSER_PASSWORD`；未连接真实 TikTok API。
