# C2 修复报告

- 原始提交保留：`07ef214 ad-management: add capability-gated API and MCP mutations`
- 前次修复提交保留：`5386465 ad-management: close C2 management gate gaps`
- 本次窄修复提交：`ad-management: make C2 gates non-bypassable`
- 未调用真实 TikTok/MCP、未部署、未执行真实广告写入。

修复内容：

1. SDK 管理请求使用独立 `management_scope`；gateway 在两次发送前把无前缀管理 operation 与 `EntityRef.kind` 传给 `verify_route`，并固定校验 tenant、BC、advertiser、连接及授权/绑定/适配器三代。MCP `BoundMCPClient` 透传实体类型并保留权限/合同 DomainError，不把门禁拒绝转换为可重试回执。
2. 普通/Smart+ 类型必须由目录显式提供，缺失、未知或冲突均 fail closed；ROAS 仅允许 adgroup，campaign 命令不发送。
3. 状态及素材状态合同要求唯一 `status` 或 `operation_status`，且值只能 `ENABLE`/`DISABLE`。
4. 删除未经真实 tools/list/schema 证据的 synthetic MCP WRITE 合同及文档声明；无观察合同返回 `management_contract_unsupported`，不发送。
5. 增加无类型、错层级、非法状态、无 MCP 合同及素材状态的双通道回归覆盖。
6. 两个管理适配器构造时强制能力回调；SDK gateway 管理回调携带无前缀 operation 与实体层级，通用 request scope 不能替代管理能力门禁。
7. campaign budget 按普通/Smart+ 分别调用官方 campaign_update 合同；ROAS 仍仅允许 adgroup，campaign ROAS 在 payload 层零发送。
8. MCP 管理适配器在 transport 缺失 observed-contract 查询能力时也 fail closed，绝不发送。

验证（隔离 PostgreSQL `tkada_c2_20261001_test`，Redis 6387/2）：

- `uv run --frozen pytest tests/integrations/tiktok/test_management_adapters.py tests/integrations/tiktok/test_mcp_protocol.py tests/modules/accounts/test_build_gateway.py -q`：76 passed
- `uv run --frozen pytest tests/modules/accounts/test_group_isolation_gateway.py -q`：测试进程完成（本地 runner 仅输出通过点，未返回汇总行）
- `uv run --frozen ruff check ...`：通过
- `uv run --frozen python -m compileall -q app/integrations/tiktok app/modules/accounts`：通过
- `git diff --check`：通过
