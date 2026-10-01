# B6 六维广告工作台阶段验收（2026-10-01）

本记录对应 B1–B5 的阶段性本地验收与查询容量证据。验收代码位于
`backend/tests/acceptance/test_reporting_workspace.py`，只使用 PostgreSQL 中的合成目录、事实和快照；没有创建 TikTok SDK/MCP 客户端，没有广告写入，也没有部署。

## 验收范围

- 验收夹具构造两个独立租户、两个独立 BC、各自的连接/账户授权和操作者。主租户同一 BC 内放置三个相同剧名、不同版权方的系列，保留 `PAUSED` 与 `DELETED` 目录状态及对应历史事实；另一租户有同名系列作为越权对照。
- 主租户包含一个没有本地文件的外部视频素材，事实通过完整 `ad_id + ad_material_id + MaterialUseRef` 证明，验证素材列表仍保留平台 VID 和广告内素材 ID。
- `test_workspace_fastapi_contract_has_no_external_write_routes` 检查 B5 FastAPI 本地工作台完整 method/path/operationId 映射（作为生成客户端静态合同）；`test_workspace_http_query_contract` 使用真实 TestClient 调用列表首屏、游标分页、趋势和详情 GET，核对 tenant/BC 参数、响应结构，并把 gateway 入口替换为计数哨兵，断言外部调用为 0。未授权 BC 的首次列表按 B3 合同返回 200 空结果，携带已有快照跨 BC 返回 404。
- `test_workspace_snapshot_contract` 覆盖 campaign/drama 列表、汇总 D0 收入/消耗/ROAS、快照趋势、`ALL_MATCHING` 全选、冻结快照导出/下载、素材行和跨租户/BC 隔离。分页先验证 2+1 行；快照发布后新增无关目录成员，selection 和 CSV 仍只含冻结的 3 个系列。暂停/删除对象的已发布金额继续计入；同名剧按版权方分开；错误 BC 返回拒绝。
- `test_workspace_capacity_has_no_n_plus_one` 精确插入并回读 1,000 个 campaign 目录行和 10,000 个 ad 目录行，仅量测本地 `build_dimension_rows` 的 campaign 集合查询。测试记录 SQL 语句数和耗时，并要求 SQL 数量保持固定上限；若随系列/广告数量线性增长即判定为 N+1。该数据量是合成容量场景，不代表真实业务规模，也不等同于带 10,000 条广告报表事实的全链路压测。

## 执行证据

| 检查 | 结果 |
| --- | --- |
| `uv run --frozen ruff check app/modules/reporting tests/modules/reporting tests/acceptance/test_reporting_workspace.py` | 通过 |
| `uv run --frozen python -m compileall -q app/modules/reporting tests/modules/reporting tests/acceptance/test_reporting_workspace.py` | 通过 |
| `git diff --check` | 通过 |
| `uv run --frozen pytest tests/acceptance/test_reporting_workspace.py -q` | 未收集：测试守卫拒绝当前环境，缺少命名为专用 PostgreSQL 测试库的 `DATABASE_URL`（exit 4） |
| `uv run --frozen pytest tests/modules/reporting tests/acceptance/test_reporting_workspace.py -q` | 同上，未启动任何测试用例 |
| `uv run --frozen ty check app` | 未通过；报告的是 B6 之前已有的 reporting/ads 全仓类型诊断，未修改这些实现 |
| Bun/前端 B5 验证 | 当前 shell 没有 `bun`，本轮未伪造运行前端命令 |

因此本轮没有可报告的真实 PostgreSQL SQL 次数、耗时、通过数或隔离数据量；容量阈值已写入验收测试，待注入独立 `DATABASE_URL` 后执行并把实际数值补入本节。当前未将环境拒绝计为实现失败，也未把静态检查结果冒充数据库验收通过。

## 证据边界与后续运行

- 测试使用真实 SQLModel/PostgreSQL session 时，会执行迁移并由根 `tests/conftest.py` 回滚每个测试；不连接 TikTok/MCP。外部调用计数应保持为零，因为验收路径只调用本地 reporting/query/export 服务。
- 运行 B6 数据库验收前，需要在 `backend/` 注入独立测试数据库（数据库名含 `test` 段）及非应用 Redis 库，然后执行：

  ```bash
  uv run --frozen pytest tests/acceptance/test_reporting_workspace.py -q
  uv run --frozen pytest tests/modules/reporting tests/acceptance/test_reporting_workspace.py -q
  uv run --frozen ruff check app/modules/reporting tests/modules/reporting tests/acceptance/test_reporting_workspace.py
  uv run --frozen ty check app
  ```

- 真实 TikTok/API/MCP 只读联调、采集周期、部署配置和生产规模均不属于本次 B6 证据；仍需按 BC 选择与授权边界单独验收。
