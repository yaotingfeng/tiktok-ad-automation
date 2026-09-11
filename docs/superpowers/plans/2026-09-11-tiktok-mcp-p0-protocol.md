# TikTok MCP P0 协议与公共调用边界 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 用户按需使用 Superpowers 的偏好优先；本文件是计划，不表示已执行。

**Goal:** 建立有官方来源的 MCP 协议配置、稳定业务上下文和可离线验证的调用边界，解除后续授权/素材/广告模块对 Agent 的依赖。

**Architecture:** 先核实公开协议和文档契约，再接固定版本官方 MCP Python 客户端。公共传输通过注入授权与准入回调接入后续租户模型，不依赖尚未实现的 P1。真实授权后的 schema 与权限事实在 P1 发布，P0 离线通过不开放业务写入。

**Tech Stack:** Python 3.14、uv、官方 MCP Python SDK v2、httpx2、Pydantic、pytest、现有 PostgreSQL/Redis 测试设施。

**Spec:** [已批准双通道设计](../specs/2026-09-11-tiktok-dual-channel-mcp-design.md)。执行前同时阅读[总览](2026-09-11-tiktok-mcp-implementation.md)。

## Global Constraints

- `OFFICIAL_API` 使用现有固定版本 TikTok Python SDK，`OFFICIAL_MCP` 使用固定版本官方 MCP Python 客户端。
- MCP 由 Python 后端直接调用，工具、参数和顺序均由代码确定。
- 第一版固定 `https://business-api.tiktok.com/open_mcp/tt-ads-mcp-flat`；用户不输入任意 MCP URL。
- 租户管理员自行授权、明确绑定 BC；P0 不读取 Codex token，不执行注册/授权/刷新/广告等真实写操作。
- 请求结果未知时不重新发送同一副作用请求；客户端 request ID 不作为上游幂等键。
- root `uv.lock`、root `bun.lock` 为锁文件；不创建 backend/uv.lock。

## 文件职责与前置条件

| 文件 | 职责 |
| --- | --- |
| `backend/app/integrations/tiktok/contracts/context.py` | 冻结通道和租户路由，不含凭据 |
| `backend/app/integrations/tiktok/contracts/common.py` | 调用证据及副作用错误语义 |
| `backend/app/integrations/tiktok/mcp/protocol.py` | 已核实协议事实读取、schema 契约校验 |
| `backend/app/integrations/tiktok/mcp/protocol-profile.json` | 公开协议事实及来源；不保存客户端密钥 |
| `backend/app/integrations/tiktok/mcp/tool-contracts.json` | 应用操作到官方工具的白名单映射和预期 schema |
| `backend/app/integrations/tiktok/mcp/results.py` | 官方 MCP 结果到业务 envelope 的严格解码 |
| `backend/app/integrations/tiktok/mcp/transport.py` | 受控异步会话、同步 worker 调用边界，不含业务编排 |
| `backend/app/integrations/tiktok/admission.py` | 复用 Redis 准入；API/MCP 共享配额域选择 |
| `backend/scripts/inspect_tiktok_mcp_protocol.py` | 只读公开 metadata 检查与脱敏输出 |
| `docs/integrations/tiktok-mcp-protocol.md` | 来源、版本、可证明保证及未能证明的限制 |

执行 Python 测试前按总览准备命名含 `_test` 的独立数据库和非零 Redis 测试 DB。下列 pytest/uv 命令除显式声明外在 `backend/` 执行，Git 命令在应用仓库根执行。

## Task P0.1: 固定公开协议与能力合同

**Files:** Create 上表 `mcp/protocol.py`、两个 JSON、探测脚本、协议文档及 `backend/tests/integrations/tiktok/test_mcp_protocol.py`；Modify `backend/pyproject.toml`、root `uv.lock`、`docs/engineering-baseline.md`。创建包所需的空 `__init__.py`，不新增通用插件框架。

**Interfaces:**

- Produces `load_mcp_protocol() -> McpProtocolProfile`；纯解析入口 `parse_profile(raw: dict[str, Any]) -> McpProtocolProfile`。
- Produces `ToolContract(operation: str, tool_name: str, effect: Literal['READ','WRITE'], input_schema: dict[str, Any], output_schema: dict[str, Any] | None, response_shape: Literal['OBJECT','OBJECT_LIST'], source_urls: tuple[str, ...])`。
- Produces `verify_tool_schema(expected: ToolContract, observed: dict[str, Any]) -> None`；不一致抛 `DomainError('mcp_contract_changed', ...)`。
- 协议事实包含 endpoint、issuer、resource、authorization/token/registration/revocation endpoint、SDK/协议版本、PKCE 方法、token auth method、refresh semantics、权限证据来源及 schema manifest SHA-256。官方未提供的可选能力为 `None`/明确 `UNVERIFIED`，不能用虚构值填充。

- [x] **Step 1: 编写协议白名单失败用例。** 样例中的域名只用于离线断言，不进行真实请求；补充缺少 issuer/resource、官方未提供 refresh 重放保证、必需 tool 参数删除的用例。

```python
import pytest
from app.core.errors import DomainError
from app.integrations.tiktok.mcp.protocol import require_official_endpoint

def test_endpoint_must_be_the_configured_official_service():
    with pytest.raises(DomainError) as exc:
        require_official_endpoint('https://business-api.tiktok.com.evil.example/mcp')
    assert exc.value.code == 'mcp_endpoint_invalid'
```

- [x] **Step 2: 运行用例，确认当前因模块不存在失败。** `uv run pytest tests/integrations/tiktok/test_mcp_protocol.py -q`。记录失败原因；不得把测试数据库配置错误算作预期红灯。
- [x] **Step 3: 核实依赖与公开文档，生成协议事实。** 仓库根执行 `uv add --package app 'mcp>=2,<3' httpx2`，记录 lock 中精确版本，再 `uv sync --frozen --package app`。本计划使用已查阅的 v2 `Client` API；若解析版本不提供该 API，先调整并验证本计划的导入，不混用 v1 示例。

```python
from urllib.parse import urlsplit
from app.core.errors import DomainError

OFFICIAL_ENDPOINT = 'https://business-api.tiktok.com/open_mcp/tt-ads-mcp-flat'

def require_official_endpoint(value: str) -> str:
    parsed = urlsplit(value)
    if value != OFFICIAL_ENDPOINT or parsed.username or parsed.password:
        raise DomainError('mcp_endpoint_invalid', 'MCP 服务配置无效')
    return value
```

探测脚本仅实现 `--metadata-only --out PATH`。读取官方文档公开链接或授权挑战所指向的公开 resource/authorization metadata；请求设连接/读取期限、限制重定向到已核实官方 issuer，不携带任何凭据，不调用 `tools/call`，不注册客户端。输出仅公共字段和来源。禁止对本机任意缓存配置做批量导出。

在应用根执行：`uv run --package app python backend/scripts/inspect_tiktok_mcp_protocol.py --metadata-only --out .runtime/mcp-protocol-public.json`。将检查后的公共事实写入协议文档与 profile；公开数据不足的能力标为未核实，并给出需在 P1 授权后验证的具体字段。不要把协议未提供的 token 有效期写死为普通 TikTok OAuth 值。

`tool-contracts.json` 从官方文档/已提供工具定义整理操作映射，覆盖账户、场景、素材、封面、CTA、三级创建与回读。证据标为 `DOCUMENTED`；P1 使用自身授权后获得的 `tools/list` 才能标记连接级 `OBSERVED`。schema 比较保留 required/type/enum/嵌套结构，排除 description 等纯说明变化。

- [x] **Step 4: 运行协议测试并检查脱敏。** `uv run pytest tests/integrations/tiktok/test_mcp_protocol.py -q`；期望全部通过，恶意 endpoint、缺少必要授权字段、schema 不兼容均被明确拒绝。检查 JSON 不含 token、注册客户端 secret 或真实账户清单。
- [x] **Step 5: 更新版本依据并提交。** 根执行 `git status -sb`、`git rev-parse --show-toplevel`；明确暂存本任务列出的文件、检查暂存差异，提交 `mcp: define verified protocol and tool contracts`。不推送。

## Task P0.2: 定义共享上下文、证据及严格结果解析

**Files:** Create `contracts/context.py`、`contracts/common.py`、`mcp/results.py`、`backend/tests/integrations/tiktok/test_mcp_results.py`；Modify `mcp/protocol.py`（连接已观察 schema 的数据结构）。

**Interfaces:**

```python
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID
from pydantic import BaseModel, ConfigDict
from app.core.errors import DomainError

ChannelKind = Literal['OFFICIAL_API', 'OFFICIAL_MCP']

class FrozenTikTokRoute(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    tenant_id: UUID
    bc_id: str
    connection_id: UUID
    channel: ChannelKind
    authorization_revision: int
    adapter_contract_revision: str

@dataclass(frozen=True)
class CallEvidence:
    request_id: str | None = None
    mcp_request_id: str | None = None
    remote_task_id: str | None = None

RemoteEffect = Literal['NOT_SENT', 'UNKNOWN', 'REJECTED_NO_EFFECT']

class RemoteCallError(DomainError):
    def __init__(self, code: str, *, effect: RemoteEffect, evidence: CallEvidence):
        super().__init__(code, 'TikTok 调用未取得可确认结果', retryable=False)
        self.effect = effect
        self.evidence = evidence

@dataclass(frozen=True)
class McpBusinessResponse:
    data: dict[str, Any] | list[dict[str, Any]]
    evidence: CallEvidence
```

`FrozenTikTokRoute` 放 context.py，其余放 common.py；`McpBusinessResponse` 是适配器内部类型，业务层只能获得各业务组的类型。`decode_mcp_result(result: CallToolResult, *, contract: ToolContract) -> McpBusinessResponse` 放 results.py。`RemoteCallError` 不自行触发任何重试，业务任务根据 effect 与原 attempt 决定后续。

- [ ] **Step 1: 编写自然语言成功不能当回执的用例。** 同时覆盖 `is_error=True`、business code 非零、data 类型不符、文本 JSON 与 structured content 冲突、未知字段不进入日志等情况。测试用 ToolContract 明确构造本地合成协议，不能冒充官方已观察字段。

```python
import pytest
from mcp.types import CallToolResult, TextContent
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.integrations.tiktok.mcp.protocol import ToolContract
from app.integrations.tiktok.mcp.results import decode_mcp_result

def test_success_text_without_business_receipt_is_unknown():
    contract = ToolContract(
        operation='builds.create_campaign', tool_name='fixture_create',
        effect='WRITE', input_schema={'type': 'object'}, output_schema=None,
        response_shape='OBJECT', source_urls=('https://example.com/fixture',),
    )
    result = CallToolResult(content=[TextContent(type='text', text='创建成功')])
    with pytest.raises(RemoteCallError) as exc:
        decode_mcp_result(result, contract=contract)
    assert exc.value.effect == 'UNKNOWN'
```

- [ ] **Step 2: 确认测试失败于待实现结果契约。** `uv run pytest tests/integrations/tiktok/test_mcp_results.py -q`。
- [ ] **Step 3: 实现共享类型与结果解码。** 按上述类型分文件实现。优先验证 structured content；仅当契约明确允许文本 JSON 且恰有一个完整业务 envelope 时解析。检查 `is_error`、已核实 business code、data 类型；提取白名单 ID 字段。禁止依靠消息文字判断是否可以重发。

```python
def require_business_success(raw, evidence):
    if not isinstance(raw, dict) or type(raw.get('code')) is not int:
        raise RemoteCallError('mcp_response_invalid', effect='UNKNOWN', evidence=evidence)
    if raw['code'] != 0:
        # 未证明无副作用的业务错误，不能交给 worker 自动重发。
        raise RemoteCallError('mcp_business_error', effect='UNKNOWN', evidence=evidence)
    return raw
```

真实服务 envelope 与此示例不同则以 P0.1 核实的契约进行适配；在该契约内编写同等严格的分支，不能把非零错误一概标为 `REJECTED_NO_EFFECT`。合成测试显式覆盖每种已支持 envelope，不引入宽松的任意 dict 兜底。

- [ ] **Step 4: 回归协议/结果测试。** `uv run pytest tests/integrations/tiktok/test_mcp_protocol.py tests/integrations/tiktok/test_mcp_results.py -q`。期望正常对象/对象数组保持 ID 精度，错误结果保留 UNKNOWN，凭据/素材 URL 不出现在异常字符串。
- [ ] **Step 5: 明确暂存相关文件并提交。** `mcp: normalize typed call evidence and outcomes`，提交前执行仓库根与暂存差异检查。

## Task P0.3: 官方 MCP 会话与每次调用边界

**Files:** Create `mcp/transport.py`、`backend/tests/integrations/tiktok/mcp_wire.py`、`test_mcp_transport.py`；Modify `backend/app/core/logging.py`（仅新增 MCP 库日志脱敏配置）。

**Interfaces:**

- `BoundMCPClient.call(*, operation: str, advertiser_id: str | None, arguments: dict[str, Any], deadline: datetime | None = None) -> McpBusinessResponse`。
- 受限目录方法 `BoundMCPClient.list_tools(*, cursor: str | None) -> ObservedToolPage`，`ObservedToolPage(tools: tuple[mcp.types.Tool, ...], next_cursor: str | None)` 为 frozen dataclass，仅供授权 bootstrap 做目录观测。尚无 observed schema 的候选会话只允许该方法，业务 call 一律 NOT_SENT；不把 tools/list 暴露给产品通用调用入口。
- 构造参数为固定 endpoint、仅内存 token、整体 `task_deadline`、`authorize(advertiser_id, operation) -> None`、`admit(advertiser_id, operation) -> AbstractContextManager[None]`、`ToolContract` 映射及连接已观察 schema。生产回调由 P1 工厂绑定，P0 测试使用断言回调。
- `open_bound_mcp_client(...) -> AbstractContextManager[BoundMCPClient]` 在同步 prefork 任务作用域内管理官方 async 客户端；不得从 ASGI event loop 直接阻塞调用。
- `mcp_wire.py` 创建仅用于测试的真实 HTTP 协议边界。`McpWire` 提供 `url`、`calls: list[dict[str, Any]]`、`enqueue_result(tool: str, result: CallToolResult)`、`disconnect_after_accept(tool: str)`；handler 覆盖 SDK 的协商/初始化、tools/list、tools/call、会话关闭。业务函数不替换为假的成功结果。

- [ ] **Step 1: 编写发送一次后断线的测试。** fixture `mcp_wire` 和 `bound_client` 定义在本任务新建 `backend/tests/integrations/tiktok/conftest.py`：后者绑定固定截止时间、合成 token、已观察合成 schema 与计数授权/准入回调；scope 为 function，并释放其本地线程/端口。例中调用名只来自 fixture 合同。

```python
def test_disconnect_after_accept_does_not_replay(bound_client, mcp_wire):
    mcp_wire.disconnect_after_accept('fixture_create')
    with pytest.raises(RemoteCallError) as exc:
        bound_client.call(operation='builds.create_campaign', advertiser_id='123',
                          arguments={'advertiser_id': '123'})
    assert exc.value.effect == 'UNKNOWN'
    calls = [c for c in mcp_wire.calls if c.get('method') == 'tools/call']
    assert len(calls) == 1
```

补充错误 advertiser、schema 不符、第一次/准入后第二次授权撤销均不发送；不同连接不共用会话；deadline 不能延长；客户端关闭失败不丢失此前返回给业务层的回执。测试文件导入 pytest、RemoteCallError 与本任务 fixture。

- [ ] **Step 2: 运行并记录红灯。** `uv run pytest tests/integrations/tiktok/test_mcp_transport.py -q`。HTTP fixture 记录真实 SDK JSON-RPC 消息，测试不能仅 mock `call_tool`。
- [ ] **Step 3: 用官方 SDK 建会话，显式控制重试和期限。** 经版本核实后使用下列 v2 接入形状，token 不进入默认 repr。

```python
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

async def call_verified_tool(endpoint, token, tool_name, arguments):
    async with httpx2.AsyncClient(
        headers={'Authorization': f'Bearer {token}'},
        timeout=httpx2.Timeout(10.0, read=30.0),
    ) as http_client:
        transport = streamable_http_client(endpoint, http_client=http_client)
        async with Client(transport) as client:
            return await client.call_tool(tool_name, arguments)
```

该片段只展示 SDK 连接形状；实际 `open_bound_mcp_client` 用受控 blocking portal 为整个任务持有一份会话，每个 `call` 都执行：验证截止时间/工具/参数 → 校验权限 → 获取准入 → 再核实权限 → 发出一次调用 → 严格解析 → 将结果返回业务层 → 释放单次准入。网络/门户清理错误在已发送后归 UNKNOWN；获得回执后由上层先持久化，再退出任务会话。设置整个操作 timeout，不能只依赖 HTTP socket timeout；显式 deadline 与任务截止取更早值。

将 `httpx2`、MCP transport 的 HTTP bodies、headers、URL debug 日志关闭或仅记录白名单错误码。SDK 如存在自动 401 refresh-and-replay，业务调用路径禁用该行为：P1 在发送前刷新，已发送后不重放原业务请求。同源重定向是否会重发 POST 也用 fixture 验证；固定最终 endpoint，非幂等业务不自动重定向重发。

目录观测沿用同一官方 SDK 会话，通过 `(None, 'protocol.list_tools')` 的授权/准入回调；逐页保存 cursor 与 schema 摘要，拒绝重复 cursor。接收层设置每个完整业务结果/目录页最多 8 MiB 的本地资源预算，流式读取也累计计数；这是本地保护阈值，不声称为 TikTok 服务限制。超限不得把截断数据解析成完整页；写入响应超限仍是 UNKNOWN。

- [ ] **Step 4: 运行传输及协议测试。** `uv run pytest tests/integrations/tiktok/test_mcp_protocol.py tests/integrations/tiktok/test_mcp_results.py tests/integrations/tiktok/test_mcp_transport.py -q`。用日志捕获断言合成 token 和 URL 未出现；真实 SDK 发送次数与会话隔离断言通过。
- [ ] **Step 5: 提交。** 明确暂存所列文件并提交 `mcp: add bounded official client transport`，记录协议替身不等于真实 TikTok 联调。

## Task P0.4: 配额域与只读交接验收

**Files:** Create `backend/app/integrations/tiktok/admission.py`、`backend/tests/integrations/tiktok/test_channel_admission.py`；Modify `backend/app/jobs/admission.py`（保留现有 Lua/键算法，扩展可信 quota scope 来源）、协议文档。

**Interfaces:**

- `quota_scope(*, channel: ChannelKind, app_id: str | None, verified_service_scope: str | None) -> str`。
- `admit_tiktok_call(redis_client: Redis, *, route: FrozenTikTokRoute, advertiser_id: str | None, operation: str, scope: str, policy: AdmissionPolicy) -> AbstractContextManager[None]`，拒绝仍使用现有 `AccountAdmissionDeferred` 的重新调度语义。
- 候选尚无 BC route，提供 `admit_candidate_call(redis_client: Redis, *, tenant_id: UUID, attempt_id: UUID, scope: str, operation: str, policy: AdmissionPolicy) -> AbstractContextManager[None]`；复用同一底层桶与租约函数，限定协议/目录只读操作，不伪造 connection/BC。attempt_id 仅做本地关联，不拆分上游配额。
- P1 连接/候选工厂构造 callback 后调用本函数；P0 不依赖 `modules/accounts/routing.py`。

- [ ] **Step 1: 验证不同连接共用未知上游额度。** 创建下列纯策略用例，以及真实 Redis 中“同服务、不同连接第二个超额调用被拒绝”的用例；Redis fixture 使用现有 `redis_client`，只清理本测试创建的前缀键。

```python
def test_unknown_mcp_upstream_uses_shared_scope():
    from app.integrations.tiktok.admission import quota_scope
    a = quota_scope(channel='OFFICIAL_MCP', app_id=None, verified_service_scope=None)
    b = quota_scope(channel='OFFICIAL_MCP', app_id='irrelevant', verified_service_scope=None)
    assert a == b == 'official-mcp:shared-unverified'
```

- [ ] **Step 2: 运行新用例确认缺少实现失败。** `uv run pytest tests/integrations/tiktok/test_channel_admission.py -q`。
- [ ] **Step 3: 实现范围选择及准入适配。** API 路径要求 app ID，MCP 不读取本产品 App 配置；可信 service scope 只能由部署/已验证协议来源提供，不能接受前端任意字段。复用原 admit/release、nonce 与 lease 语义，不重新实现 Lua。

```python
def quota_scope(*, channel, app_id, verified_service_scope):
    if channel == 'OFFICIAL_MCP':
        return f'official-mcp:{verified_service_scope}' if verified_service_scope else 'official-mcp:shared-unverified'
    if not app_id:
        raise DomainError('tiktok_app_unconfigured', '官方 API 应用尚未配置')
    return app_id
```

账户为空的授权发现/会话请求使用受控 discovery 配额键，不将 `None` 当成真实 advertiser ID。MCP 无可信终止证据的 UNKNOWN 继续由业务持久化围栏阻止竞争写入；释放本地并发租约不表示远端结束。

- [ ] **Step 4: 运行公共层回归。** `uv run pytest tests/integrations/tiktok tests/modules/accounts/test_admission.py tests/jobs/test_admission.py -q`；再执行 `uv run ruff check app/integrations/tiktok tests/integrations/tiktok`。现有 accounts autouse 会填假 App，新增 MCP 无 App 用例须在测试内显式清空三个 App 配置。
- [ ] **Step 5: 完成 P0 记录并提交。** 记录具体 SDK/协议版本、公开 metadata、工具合同来源、服务端重试可证明范围及 P1 必须核实的权限/schema；提交 `mcp: scope shared admission by upstream service`。真实授权仍进入 P1 的具体授权验证步骤。

## 本阶段证据与退出条件

公共层所有测试及已有准入测试通过；应用可加载固定 SDK 和协议配置，但尚不能宣传已连接任一真实 BC。公开协议信息不足时可以完成受阻能力之外的纯契约代码，相关连接能力必须保持未核实；真实授权注册若需要用户操作，以具体环境和操作说明处理，不导入 Codex 缓存凭据。

参考：[官方 MCP 客户端](https://py.sdk.modelcontextprotocol.io/client/)、[v2 传输](https://py.sdk.modelcontextprotocol.io/client/transports/)、[OAuth 客户端](https://py.sdk.modelcontextprotocol.io/client/oauth-clients/)。这些页面用于版本与接口依据；文档示例不证明 TikTok 具体授权参数。
