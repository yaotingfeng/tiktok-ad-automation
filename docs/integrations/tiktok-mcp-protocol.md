# TikTok MCP 公共协议与工具合同

核实日期：2026-09-11。P0.1 只访问公共元数据，没有读取 Codex 缓存凭据、连接任一 BC、注册客户端、用户授权、刷新或调用业务工具。记录分为公共协议事实、离线提供的工具声明与后续连接级验证；它们不能互相替代。

## 公共协议事实

固定服务为 `https://business-api.tiktok.com/open_mcp/tt-ads-mcp-flat`。后端精确匹配此地址，不接受用户提供其他 URL、layer 端点、端口、查询参数或尾斜杠。

2026-09-11 14:27 UTC，以下两个不带凭据的 GET 均返回 HTTP 200：

- [Protected resource metadata](https://business-api.tiktok.com/.well-known/oauth-protected-resource/open_mcp/tt-ads-mcp-flat)：`resource` 等于固定服务，`authorization_servers` 仅包含下表 issuer，`scopes_supported=["mcp:tt4b"]`，bearer 传递方式为 header。
- [Authorization server metadata](https://business-api.tiktok.com/open_mcp/tt-ads-mcp-flat/oauth/.well-known/openid-configuration)：公开了下表授权端点和方法。

| 字段 | 核实值 |
| --- | --- |
| issuer | `https://business-api.tiktok.com/open_mcp/tt-ads-mcp-flat/oauth` |
| authorization_endpoint | `https://business-api.tiktok.com/portal/mcp-tt4b-authorize` |
| token_endpoint | issuer + `/token` |
| registration_endpoint | issuer + `/register` |
| revocation_endpoint | issuer + `/revoke` |
| code_challenge_methods_supported | `S256` |
| token_endpoint_auth_methods_supported | `none` |
| response_types_supported | `code` |
| grant_types_supported | `authorization_code`, `refresh_token` |

服务 GET 本身返回 405，未带授权挑战；这不能推断授权不存在。使用 [MCP 官方发现规则](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)从资源路径构造 protected-resource 地址，随后按发现的 issuer 尝试标准发现地址。路径插入的 OAuth/OpenID 地址返回 404，issuer 下 `.well-known/openid-configuration` 返回 200。探测脚本已固定成功的两条公共地址，不访问登录、注册、令牌和撤销地址，不跟随任何重定向。

`PUBLIC_METADATA` 表示公共配置可核实，不代表已授权租户或已经验证注册成功。支持的 `mcp:tt4b` 范围不等于实际授予范围，不证明 BC/账户写权限。授权代码换令牌、回调 URI 规则、注册条件、token 有效期、refresh 轮换/重放/恢复保证、撤销隔离与 grant 连续性均为 `UNVERIFIED`。没有借用普通 `tt_user` OAuth 的参数或有效期；缺少官方重放保证时 `automatic_refresh_replay_allowed=False`。

## 固定 SDK 与传输边界

实际解析并安装的是官方 [MCP Python SDK v2.2.0](https://github.com/modelcontextprotocol/python-sdk/tree/v2.2.0)，HTTP 客户端为 `httpx2==2.12.0`。backend 声明精确版本，根 `uv.lock` 锁定传递依赖；不创建 backend 锁文件。

实际导入与签名检查通过：

```python
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
import httpx2

# streamable_http_client(url, *, http_client=None, terminate_on_close=True)
```

[官方传输说明](https://github.com/modelcontextprotocol/python-sdk/blob/v2.2.0/docs/client/transports.md)的 v2 `Client` 与本次安装一致；没有混用 v1 `ClientSession` 示例。SDK 的 `LATEST_PROTOCOL_VERSION` 是 `2026-07-28`，这只是客户端实现版本。TikTok 服务器实际协商版本保持 `None`，选用 Streamable HTTP 的真实连接兼容性保持 `UNVERIFIED`。P0 不通过自动 OAuth provider 触发注册或授权，也不把 SDK 的自动版本协商或重试行为当成上游副作用幂等保证。

## 工具合同

`backend/app/integrations/tiktok/mcp/tool-contracts.json` 包含 29 个候选操作，覆盖账户/BC/角色、Identity/Minis/CTA/地区/VBO、视频与图片入库/信息/搜索、建议封面、CTA portfolio、Smart+ 三级创建与回读，以及普通 adgroup 状态补查。

来源是本次会话已提供的官方连接器工具 TypeScript 声明，离线检查，无已认证 MCP 查询。清单只转换声明中明确给出的 `required/type/enum` 与嵌套结构。连接器命名空间被去掉作为候选远端名，**只有 TK-ADA 自身连接完整 `tools/list` 的名称及 schema 匹配，才能确认为该连接可使用的映射**。清单中的 `DOCUMENTED` 不能改标为 `OBSERVED` 来表示本次连接级联调。

提供的声明存在 `unknown`、开放对象、仅在说明中给出的条件必需参数。它们保留未知，不补造封闭 JSON Schema；P1 必须用实际 `tools/list` 和正式端点说明补全。完整 input/output schema、默认值、分页和 envelope 都仍需自身授权后核对。清单不声明注册、OAuth 或广告写权限。

`ToolContract.response_shape` 仅表示本应用要求成功业务 envelope 内 `data` 的形状：`OBJECT` 或 `OBJECT_LIST`。当前候选统一记录应用要求 `OBJECT`，不是已核实的平台返回结论。没有提供完整输出 schema，因此 `output_schema=None` 且 `text_json_envelope=False`；不能据此接受任意文本 JSON 或自然语言“成功”。P1 必须核实输出和完整回读覆盖后才能开放对应业务能力。

比较器严格保留参数名、必需参数、类型、枚举、嵌套结构与已知输出 schema。忽略 description/title/examples/$comment 说明变化和 required/enum 集合顺序；同名业务字段、dependentRequired/旧版 dependencies 的属性名及 const/default/enum 内的 JSON 值仍参与比较；JSON 类型敏感比较区分布尔值 true/false 与数字 1/0，包括嵌套字面量。新增可选字段也会触发复核，未知新增工具不进入白名单。schema 不匹配抛出 `mcp_contract_changed`。profile 记录整个 manifest 原始字节的 SHA-256，加载时验证一致性；任何 manifest 编辑都必须重新审查并更新摘要。

上传声明明确包含 `auto_bind_enabled` 和 `auto_fix_enabled`，不含 `video_signature`；原件摘要身份不能因此视为已证明。Smart Fix 默认行为、URL 入库大小/超时、来源 advertiser 与目标 VID 映射、结果未知后的关联查询仍是 P1/P2 必须提供的独立证据。未采纳工具说明中“重命名后重新上传”作为结果未知恢复策略。

## 重复核实与 P1 验收

在仓库根运行：

```bash
uv run --package app python backend/scripts/inspect_tiktok_mcp_protocol.py --metadata-only --out .runtime/mcp-protocol-public.json
```

脚本只支持 metadata-only；固定两条 GET，不携带认证/cookie，不读取环境代理/本机缓存、不跟随重定向。连接时限 5 秒、单次读取时限 10 秒、每份元数据总时限 20 秒、响应体上限 128 KiB。只保存公共字段白名单及来源/HTTP 状态，不保存原始 headers、异常文本、注册响应、账户或 token。

P1 待核实：管理员自身注册与 PKCE 授权闭环；精确 callback URI 与 resource/issuer 绑定；实际 scope、授权主体及 grant 连续性；管理员明确 BC 选择；完整账户分页、角色与归属；实时工具名称/input/output schema；真实协商版本；刷新未知结果处理；远端副作用重试语义。授权主体候选来源为 TikTok for Business `user_info_get` 的字符串 `core_user_id`（非 TTO 用户接口，DOCUMENTED，仍需自身连接回读验证）；其他权限候选来源为 `bc_get`、`bc_asset_get`、`bc_member_get`、`bc_asset_member_get`、`auth_advertiser_get`；必须证明它们能关联当前授权主体并返回足够操作角色，账户可见和工具存在都不能当作写权限。
