# API 授权账户同步验证

## 变更

API 授权连接详情新增“同步全部账户”。接口为
`POST /api/tenants/{tenant_id}/tiktok/connections/{connection_id}/sync`，同步范围是当前 API 授权可见的完整 BC 和广告账户目录。

## 本地验证

- `uv run --frozen ruff check app/integrations/tiktok/official/bootstrap.py app/modules/accounts/tasks.py app/modules/accounts/router.py`：通过
- `python3 -m compileall -q app/integrations/tiktok/official/bootstrap.py app/modules/accounts/tasks.py app/modules/accounts/router.py`：通过
- `./.tools/node_modules/.bin/bun run --cwd frontend build`：通过
- 后端 PostgreSQL 回归：未执行；本机未提供命名 `_test` 的专用数据库，测试前置检查会拒绝运行。
- Biome：未执行；仓库根配置与 frontend 嵌套配置同时存在，Biome 直接退出配置错误。
