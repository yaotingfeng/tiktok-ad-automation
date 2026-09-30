# B3 修复轮报告

基于初版提交 `c7ac4d1` 完成复审修复（最终复核）：

- 查询创建路径在生产 `Session` 上使用 B1 `snapshot_transaction`（REPEATABLE READ），正常退出提交，异常退出回滚；测试连接保留外层事务，HTTP 路由显式提交本地快照，跨请求游标/快照可读。新增真实 Engine `TestClient` 回归覆盖第一页、后续游标和选择请求。
- 账户行在快照创建时按当前 BC 的已授权账户和连接批量固化系列 `EntityRef`，选择阶段只复制快照行，不重新查实时目录，也不产生 N+1；版本摘要限定租户、BC、授权连接和账户，包含事实发布版本和名称投影版本。
- 详情操作历史改为读取租户范围内真实 `AuditEvent`，无记录时返回空历史，不伪造当前目录状态为历史事件；父级限制和素材引用仍为本地目录数据。

验证（使用仓库隔离 `test.env`，未输出配置、未调用 TikTok/MCP）：

```text
uv run --frozen pytest tests/modules/reporting/test_queries.py tests/modules/reporting/test_selection.py tests/modules/reporting/test_detail.py tests/modules/reporting/test_api.py -q
14 passed in 1.93s
uv run --frozen pytest tests/modules/reporting -q
77 passed in 4.69s
uv run --frozen ruff check app/modules/reporting app/api/main.py tests/modules/reporting
All checks passed!
uv run --frozen python -m compileall -q app/modules/reporting app/api/main.py tests/modules/reporting
```

真实 Engine 回归在夹具收尾时会产生 SQLAlchemy `transaction already deassociated from connection` 提示；该提示只来自测试夹具对已提交外层连接的回滚，测试本身已用 warning filter 限定，不影响应用请求或快照持久化。

本轮保持原提交主题：`reports: add consistent queries and frozen cross-page selections`。
