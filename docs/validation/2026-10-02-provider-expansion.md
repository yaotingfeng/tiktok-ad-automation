# 2026-10-02 版权方来源扩展离线验收

本轮将兑吧、刚刚好、容量接入投放工具的版权方连接和自动取链工作流，并保留网眼、嘉书的既有行为。所有验证均使用本地 PostgreSQL/Redis、HTTP fixture 和浏览器接口替身；没有读取本机 CLI 账号文件，没有调用真实版权方接口，也没有创建 TikTok 广告。

## 已交付

- 连接类型固定为 `wangyan`、`jiashu`、`duiba`、`gangganhao`、`rongliang`；`other` 继续表示手动推广链接。
- 三个新 adapter 通过统一的搜索、历史链接核验、创建、回读和未知结果恢复合同接入 durable workflow。
- 投放页在应用验证后读取能力接口，按来源显示取链参数，并将 `link_config` 纳入创建、更新和浏览器重试请求。
- 连接管理页支持各来源凭据字段：兑吧账号、刚刚好门户 ID/用户名、容量邮箱，以及网眼/嘉书原有字段。
- OpenAPI 客户端已由后端 schema 重新生成，能力接口为：
  `GET /api/tenants/{tenant_id}/providers/connections/{connection_id}/applications/{application_id}/capabilities`。

## 离线证据

| 检查 | 结果 |
| --- | --- |
| `uv run pytest tests/modules/providers -q` | 292 passed |
| `uv run pytest tests/modules/providers/test_preparation_api.py -q` | 12 passed，含能力接口与租户/验证状态检查 |
| `npm run build`（`frontend/`） | TypeScript 与 Vite 构建通过 |
| `npx --yes --package bun bunx playwright test tests/providers.spec.ts` | 22 passed |
| 三个外部 CLI `node --check` | 全部通过 |
| `uv run alembic current` | `provider_kinds_expansion (head)` |
| `git diff --check` | 通过 |

浏览器测试使用 `npx --yes --package bun bunx`，因为验收环境没有全局 `bun` 命令；这只影响命令入口，不影响构建结果。

## 复审修复

复审发现刚刚好历史链接查询原先只按剧集和参数匹配，未把 `authorizerAppId` 纳入查询和详情核验，存在跨应用误复用链接的风险。现已要求查询配置携带应用 ID，并同时校验详情中的应用 ID、系列 ID、集数和支付模板；新增跨应用误复用回归，专项协议测试 4 项通过。

## 未纳入本轮

真实版权方账号登录、应用发现、搜索和真实创建/回读仍需在具备对应授权的环境中分来源验收。真实取链和 TikTok 广告写入必须分别确认；本轮离线 fixture 结果不替代真实服务回执。
