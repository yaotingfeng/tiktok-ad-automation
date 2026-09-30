# B4 个人视图、异步导出与手动刷新

## 实现

- 新增 `preferences.py`：保存、读取、更新和删除按租户、BC、操作者隔离的 `SavedReportView`，统一校验 `ReportingFilter` 和支持列。
- 新增 `exports.py` 与 `export_tasks.py`：创建导出时复制快照行和指标到 `ReportExport.frozen_rows`；生成与下载均复核当前租户、BC 和账户授权，产物 24 小时过期；CSV 写入表头、币种、时区、coverage 和抓取时间，公式前缀使用 `csv_safe_text`，Decimal 金额保持数字，多币种桶分行输出。生成使用原子文件替换或部署配置的既有对象存储客户端，失败不保留对象键。
- `ads-reporting` 队列注册了 `reporting.export`；刷新 API 先调用 `freeze_route`，再构造 A 的 `SyncRequest`/`request_sync`，轮询只读本地运行表。
- reporting API 增加个人视图、导出（含下载）、手动同步创建及本地状态轮询端点，所有路由保留 `ads_reporting-` operation ID 约定。

## 验证

- `uv run --frozen ruff check app/modules/reporting tests/modules/reporting/test_exports.py app/jobs/celery_app.py app/jobs/tasks.py`：通过。
- `uv run --frozen python -m compileall -q app/modules/reporting app/jobs tests/modules/reporting/test_exports.py`：通过。
- `git diff --check`：通过。
- CSV smoke check：公式前缀保护、Decimal `-2`、USD/EUR 多币种分桶：通过。
- 专用 PostgreSQL 测试库未在当前 shell 注入；`uv run --frozen pytest tests/modules/reporting/test_exports.py -q` 在收集阶段按仓库数据库守卫停止（`DATABASE_URL` 非专用测试库），未发起 TikTok/MCP/provider 请求。

## 提交

待根端复核后提交，提交前仅暂存 B4 文件，未执行 `git add .`、部署或推送。
