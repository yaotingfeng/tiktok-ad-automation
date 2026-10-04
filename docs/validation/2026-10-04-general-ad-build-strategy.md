# 通用投放策略：Task 7 离线回归验收

日期：2026-10-05
基线：`637dda9`
范围：Task 7 端到端回归夹具、迁移检查和运行文档。测试只使用本地 fixture；没有调用 TikTok/MCP 写接口。

## 离线合同与回归证据

后端新增的离线夹具覆盖四类冻结结构：

- 1 个系列、1 个广告组、按素材生成广告、每个广告 1 个素材：20 个 `PlannedAd`，每个广告的 `PreviewAdMaterial` 集合为单条素材。
- 1 个系列、2 个广告组、组层顺序平均、每个广告 1 个素材：两组各 10 个广告，组素材集合互斥且按冻结顺序切分；该 fixture 同时编译目标 ROAS，断言 `VO_MIN_ROAS` 和 `roas_bid`。
- 1 个系列、1 个广告组、固定 2 个基础广告、共用素材、`creative_count=3`：6 个最终 `PlannedAd`，两个基础广告各自冻结 20 条素材，创意复制不增加广告级素材映射。
- 1 个系列、2 个广告组、每组 10 个广告、组预算、最高价值：Campaign 使用 `BUDGET_MODE_INFINITE` 且不带系列日预算，Ad Group 带精确预算；最高价值带 `VO_HIGHEST_VALUE` 且不带 `roas_bid`。

相同策略版本被两个独立草稿引用时，测试使用不同 preview ID 生成 A/B 冻结行，行集合互斥，策略配置保持不变。共享素材执行夹具按 `(advertiser_id, material_id)` 去重准备任务，同时保留每个广告自己的 `creative_list` 和独立正文。CTA 推荐资产、目标视频和封面字段在本地编译前均有 fence。

失败与恢复夹具覆盖：目标素材不可用、封面字段待核实、组预算能力未核实、最高价值能力字段缺失，以及失败后使用同一冻结素材包重新计算。恢复前后的组数量、广告数量和广告级素材集合一致，不补素材、不重新拆组。既有执行回归继续覆盖部分广告失败和未知远端结果，未知结果只保留原尝试核查，不自动重发。

已执行并通过：

```text
uv run pytest --confcutdir=/tmp /tmp/task7_offline.py -q
7 passed in 0.16s (DB-free supplemental checks)

PATH="$PWD/.tools/node_modules/.bin:$PATH" \
  bun run --cwd frontend test -- tests/build-preparation.spec.ts \
  -g '预览展示冻结定向|当前版本已有预览' --workers=1
2 passed

新增 DB-bound 回归位于 `backend/tests/modules/builds/test_previews.py`（A/B 隔离和四结构矩阵）、`test_material_execution.py`（真实素材切片幂等）、`test_partial_material_execution.py`（不可用素材/UNKNOWN 恢复）及 `test_cover_execution.py`（pending cover 恢复冻结行）；它们未被 7 项 harness 计入。尝试运行预览 smoke 时，session migration fixture 在 PostgreSQL `127.0.0.1:15432` 连接阶段返回 5 个 `connection refused` errors，因此没有把这些 DB-bound 测试记为通过。

uv run ruff check backend/tests/modules/builds/test_execution_assets.py
All checks passed

uv run python -m compileall -q backend/tests/modules/builds/test_execution_assets.py
passed

git diff --check
passed

node_modules/.bin/tsc -p frontend/tsconfig.build.json --noEmit
passed
```

前端 Task 7 两个聚焦场景在本地 Playwright workspace 项目通过；本次没有启动真实 API、TikTok 或 MCP 连接。

## 迁移与环境边界

`uv run alembic heads` 可解析迁移图，当前有两个并行 head：

- `20261005_preview_ad_material_strategy`
- `provider_kinds_expansion`

不能在当前环境宣称升级/降级已验收：测试 PostgreSQL `127.0.0.1:15432` 连接被拒绝，Alembic `check` 和 pytest 的 session migration fixture 均在连接阶段失败。Redis 未启动，因此真实 PostgreSQL/Redis 并发、素材准备任务去重、部分失败恢复和迁移 upgrade/downgrade 尚未执行。没有 staging 预览证据，本记录不把离线测试称为 staging 或生产验收。

完整前端命令曾手动中断；最终记录为 **44 passed / 13 failed / 1 interrupted / 60 not run**。失败来自既有策略选择器/草稿 fixture，不能作为本轮全量通过证据。聚焦的新增/受影响场景单独通过。Bun 使用仓库已有 `.tools/node_modules/.bin/bun`（1.4.2）。

项目级 `node .codex/skills/tiktok-smart-plus-drama-ads/scripts/test-batch-tools.mjs`、完整 backend pytest、完整 frontend test/build 和迁移 upgrade/downgrade 应在专用 PostgreSQL、Redis 和完整环境配置可用后重跑。

## Staging 与真实平台授权

- Staging 预览验收：**无**。本轮没有连接测试环境，也没有把本地 fixture 当作 staging 结果。
- 真实 TikTok/MCP 读回：**未执行**。
- Campaign、Ad Group、Ad 创建或启用写入：**未执行**，没有发送真实广告写请求。
- 生产验收：**不适用/未宣称**。完成 staging 迁移、预览、读回和恢复验证，并获得单独的真实平台写入授权后，才可进行下一轮验收。
