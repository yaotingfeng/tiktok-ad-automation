# TikTok MCP P2 素材闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在固定租户连接上完成双通道视频/图片读取、R2 URL 入库、封面和目标素材准备，保留未知结果及原件保护。

**Architecture:** P1 提供固定路由、授权核实和任务作用域 gateway；本阶段补齐 `gateway.materials`。业务状态机继续拥有请求围栏、回执和原件引用，适配器只执行一次已授权操作并转换业务 DTO；来源连接与目标连接独立冻结。

**Tech Stack:** Python 3.14、uv、SQLModel/Alembic、PostgreSQL、Redis/Celery prefork、固定版本 TikTok Python SDK、P0 锁定的官方 MCP Python SDK、现有 S3/R2。

**Spec:** [双通道设计](../specs/2026-09-11-tiktok-dual-channel-mcp-design.md)，重点第 6–9 节；同时阅读 [R2 原件设计](../specs/2026-09-10-r2-transient-video-upload-design.md)。依赖 P0 工具合同和 P1 连接/配额实现；向 P3 提供目标素材与封面准备边界。

## Global Constraints

- 上传不绑定剧目；保存实际来源账户及目标素材 ID；不引入素材改名重传或跨通道补传。
- 同一预览/提交派生的任务继承父级冻结目标执行连接，来源连接不能替换目标执行连接。
- SDK/MCP 参数不假定等价；缺少摘要、内容身份、不自动修复或权限证据时阻止对应路径。
- 本地 worker、签名 URL、Redis 租约到期均不证明远端请求结束；未知用途持续计入容量预算。
- 1 GiB 是应用单文件容量，不是已验证 MCP 上限；MCP 实际上限必须有独立证据。
- 外部替身只在传输边界；锁、并发、claim、清理使用真实 PostgreSQL/Redis，测试配置必须通过 `tests/database.py` 的隔离校验。
- 本文只授权未来本地实施步骤；当前不安装 SDK、不迁移数据库、不调用真实 TikTok/R2、不提交或部署。

## 文件与共享接口

新建 `backend/app/integrations/tiktok/contracts/materials.py`（DTO/Protocol）、`adapters/sdk_materials.py`、`adapters/mcp_materials.py`（通道实现）及 `backend/app/modules/materials/channel_policy.py`（已验证容量/内容策略）。

修改现有 `sdk_assets.py`、`cover_sdk.py`、`source_uploads.py`、`source_url_uploads.py`、`distribution.py`、`covers.py`、`remote_sources.py`、`readiness.py`、`source_selection.py`、`tasks.py`、`object_uses.py` 和清理调用方；不重写现有 ingest 或调度系统。

下文未写前缀的素材生产文件均在 `backend/app/modules/materials/`，素材测试均在 `backend/tests/modules/materials/`；gateway.py 为 `backend/app/integrations/tiktok/gateway.py`。pytest/ruff/ty/Alembic 命令从 backend 执行；提交前从仓库根执行 `git status -sb`、`git rev-parse --show-toplevel`，明确暂存本任务文件，不推送。

P0 已定义 `FrozenTikTokRoute(tenant_id, bc_id, connection_id, channel, authorization_revision, adapter_contract_revision)`；`freeze_route(session, *, context, bc_id, connection_id=None)` 只用于新准备操作；`verify_route(session, *, context, route, advertiser_id, capability)` 用于每次发送前。工厂 task_deadline 使用本次 RemoteCallBudget.deadline。`open_tiktok_gateway(*, database_engine, redis_client, context, route, task_deadline)` 返回 context manager，内部负责真实调用准入；业务层不再套第二层旧 App-ID 准入。

MCP 只调用 P0 `mcp/transport.py` 的 `BoundMCPClient.call(*, operation: str, advertiser_id: str | None, arguments: dict[str, Any], deadline: datetime | None = None) -> McpBusinessResponse`；响应为 `data` 与 `evidence: CallEvidence`。操作映射来自 `mcp/tool-contracts.json`，协议事实来自 `mcp/protocol-profile.json` / `load_mcp_protocol()`，不能猜远端工具名。`contracts/common.py` 的 `RemoteCallError.effect` 取 `NOT_SENT`、`UNKNOWN`、`REJECTED_NO_EFFECT`；错误证据只使用公共 CallEvidence。

以下契约是 P2/P3 的唯一素材类型来源。`SourcePreview`、`RemoteCallBudget` 从 `modules/materials/sdk_assets.py` 原样移动；`VideoCover`、`ImageReceipt` 从 `cover_sdk.py` 移动，`ImageReceipt` 增加可空调用证据字段。更新所有生产及测试 import，不留旧路径 re-export。`RemoteCallBudget` 的 deadline 只承诺本地调用预算，不声称能取消远端。

```python
# contracts/materials.py；以下 dataclass 均 frozen=True，URL/摘要字段 repr=False。
from dataclasses import dataclass, field
from typing import Protocol
from app.integrations.tiktok.contracts.common import CallEvidence

@dataclass(frozen=True)
class VideoRecord:
    advertiser_id: str
    video_id: str
    mid: str | None
    md5: str | None = field(repr=False)
    file_name: str | None
    width: int | None
    height: int | None
    size: int | None
    duration: float | None
    format: str | None
    displayable: bool | None
    evidence: CallEvidence

@dataclass(frozen=True)
class ImageRecord:
    advertiser_id: str
    image_id: str
    signature: str | None = field(repr=False)
    file_name: str | None
    width: int | None
    height: int | None
    displayable: bool | None
    evidence: CallEvidence

@dataclass(frozen=True)
class MaterialPage[T]:
    rows: tuple[T, ...]
    page: int
    page_size: int
    total_pages: int
    total_number: int | None
    evidence: CallEvidence

@dataclass(frozen=True)
class VideoReceipt:
    video_id: str
    mid: str | None
    evidence: CallEvidence

@dataclass(frozen=True)
class URLVideoUpload:
    advertiser_id: str
    url: str = field(repr=False)
    file_name: str
    expected_md5: str = field(repr=False)
    byte_size: int

@dataclass(frozen=True)
class FileVideoUpload:
    advertiser_id: str
    local_path: str = field(repr=False)
    file_name: str
    expected_md5: str = field(repr=False)
    byte_size: int

@dataclass(frozen=True)
class URLImageUpload:
    advertiser_id: str
    url: str = field(repr=False)
    file_name: str

class MaterialOperations(Protocol):
    def read_video(self, *, advertiser_id: str, video_id: str, budget: RemoteCallBudget) -> VideoRecord | None: ...
    def read_source_preview(self, *, advertiser_id: str, video_id: str, budget: RemoteCallBudget) -> SourcePreview: ...
    def search_videos(self, *, advertiser_id: str, page: int, material_ids: tuple[str, ...], budget: RemoteCallBudget) -> MaterialPage[VideoRecord]: ...
    def upload_video_url(self, request: URLVideoUpload, *, budget: RemoteCallBudget) -> VideoReceipt: ...
    def upload_video_file(self, request: FileVideoUpload, *, budget: RemoteCallBudget) -> VideoReceipt: ...
    def read_video_cover(self, *, advertiser_id: str, video_id: str, md5: str, budget: RemoteCallBudget) -> VideoCover: ...
    def suggest_cover(self, *, advertiser_id: str, video_id: str, width: int, height: int, budget: RemoteCallBudget) -> VideoCover | None: ...
    def upload_image_url(self, request: URLImageUpload, *, budget: RemoteCallBudget) -> ImageReceipt: ...
    def read_image(self, *, advertiser_id: str, image_id: str, budget: RemoteCallBudget) -> ImageRecord | None: ...
    def search_images(self, *, advertiser_id: str, page: int, budget: RemoteCallBudget) -> MaterialPage[ImageRecord]: ...
```

`ImageReceipt(image_id: str, signature: str | None, evidence: CallEvidence = field(default_factory=CallEvidence))` 不代表已核实可用。列表候选允许缺少媒体属性，以 `None` 表示；完整详情必须经现有 `verified_video`/`verified_image` 的严格身份规则才可发布映射。不将缺失 MD5 填成请求摘要。`MaterialPage.total_number=None` 只用于既有视频端点未返回数量的完整页码契约；图片搜索继续要求数量证据，不能伪造总数。

## Task 1: 提取素材契约与双通道只读适配

**Files:** Create 上述 contracts/materials.py、adapters/sdk_materials.py、adapters/mcp_materials.py；Modify sdk_assets.py、cover_sdk.py、remote_sources.py、catalog.py、gateway.py；Test `backend/tests/integrations/tiktok/test_material_contracts.py`、现有 `test_sdk_assets.py`、`test_cover_sdk.py`。

**Interfaces:** 提供上述 Protocol；SDK 类 `SDKMaterialOperations` 与 MCP 类 `MCPMaterialOperations` 由 P1 gateway 创建。新建纯函数 `video_identity(record: VideoRecord | None, *, advertiser_id: str, video_id: str, md5: str, expected_size: int) -> dict[str, str] | None` 于 `modules/materials/sdk_assets.py`，用于现有发布规则，返回实际 `video_id` 和存在时的 `mid`。

本任务先于 P2 写入及 P3 创建执行，可与 P3 只读任务一起完成设计 P1 的完整只读里程碑。MCP 写方法只实现 `raise RemoteCallError('material_channel_unverified', effect='NOT_SENT', evidence=CallEvidence())`；不会出网。SDK 写方法在 Task 3 迁移前保持原入口，Task 3 完成后删除旧入口，不能留下长期 fallback。

- [ ] 增加真实失败测试，完整列出测试 imports；该测试只检验业务证据，不模拟 workflow：

```python
from dataclasses import replace
from app.integrations.tiktok.contracts.common import CallEvidence
from app.integrations.tiktok.contracts.materials import VideoRecord
from app.modules.materials.sdk_assets import video_identity

def test_video_identity_never_uses_requested_digest_as_remote_evidence():
    row = VideoRecord("a", "v", "m", None, "fixed.mp4", 1080, 1920,
                      120, 4.5, "mp4", True, CallEvidence(request_id="req"))
    assert video_identity(row, advertiser_id="a", video_id="v", md5="a" * 32, expected_size=120) is None
    verified = replace(row, md5="a" * 32)
    assert video_identity(verified, advertiser_id="b", video_id="v", md5="a" * 32, expected_size=120) is None
    assert video_identity(verified, advertiser_id="a", video_id="v", md5="a" * 32, expected_size=120) == {"video_id": "v", "mid": "m"}
```

- [ ] 在 backend 执行 `uv run pytest tests/integrations/tiktok/test_material_contracts.py -q`，预期缺少新接口而失败。
- [ ] 移动 DTO；SDK adapter 移入真实请求代码和 SDK models；MCP adapter 经 P0 校验后的调用层转换其实际 envelope。将现有纯解析器变成 DTO 转换/验证，`video_identity` 先检查账户和 VID，再用 `verified_video` 已有宽高/大小/时长/格式/displayable/MD5 规则。结构缺损抛已有安全领域错误，详情空列表返回 None，多个冲突详情不挑第一条。实现核心：

```python
def video_identity(record, *, advertiser_id, video_id, md5, expected_size):
    if record is None or (record.advertiser_id, record.video_id) != (advertiser_id, video_id):
        return None
    from dataclasses import asdict
    row = asdict(record)
    row["signature"], row["material_id"] = row.pop("md5"), row.pop("mid")
    return verified_video({"list": [row]}, md5=md5,
                          expected_video_id=video_id, expected_size=expected_size)
```

- [ ] 为两个真实 adapter 复用 `test_sdk_assets.py`/`test_cover_sdk.py` 的响应用例；API 保留 urllib3 替身，MCP 使用 P0 McpWire 的真实本地 HTTP 协议边界回放脱敏合同 fixture，不 mock session.call_tool。覆盖空列表、错误码、跨页重复 ID、缺 page_info、数字 ID、未知字段与文本 JSON envelope；不 mock MaterialOperations 或 gateway。搜索候选不能直接发布 available。
- [ ] MCP 构造函数接收 `BoundMCPClient`，读取方法按 `client.call(operation=合同中的素材操作键, advertiser_id=advertiser_id, arguments=合同允许字段, deadline=budget.deadline)` 调用；P0 mapping 验证通过才可调用。两个适配器将 CallEvidence 与已校验 DTO 一起返回，所有 SDK model 留在 SDK adapter。
- [ ] 执行 `uv run pytest tests/integrations/tiktok/test_material_contracts.py tests/modules/materials/test_sdk_assets.py tests/modules/materials/test_cover_sdk.py tests/modules/materials/test_read_apis.py -q`；预期通过，read API 不产生 outbox 或写请求。实际执行时按明确文件暂存并提交 `materials: extract typed TikTok read operations`。

## Task 2: 固定目标路由与来源依赖

**Files:** Modify `backend/app/modules/builds/{execution,material_execution}.py` 及相应测试、distribution.py、source_selection.py、source_url_uploads.py、readiness.py、models.py、cover_models.py、tasks.py、`backend/app/core/errors.py`；Create `backend/app/alembic/versions/mcp_material_routes_material_frozen_routes.py`；Test `test_remote_only_distribution.py`、`test_distribution_concurrency.py`、新建 `backend/tests/modules/materials/test_material_route_migration.py`。

**Interfaces:** 依赖已前置完成的 P3 Task 2 `load_preview_route(session,*,context,preview_id)->FrozenTikTokRoute`；提供 `ensure_target_asset(session, *, context, bc_id, material_id, advertiser_id, task_key, route: FrozenTikTokRoute) -> AssetPreparation`；`ensure_cover` 增加同名必需参数。现有 `run_source_upload`/`run_distribution` 外部任务参数保持 ID/代次，由数据库加载冻结路由；新增创建入口必须传 route，恢复入口不得 freeze 默认。

- [ ] 修改已有测试 queue helper，在已有 session 中调用 `freeze_route(session, context=env['context'], bc_id=env['bc_id'], connection_id=env['connection_id'])` 明确创建 route 并传给 ensure_target_asset；新增以下数据库断言，复用 test_remote_only_distribution 的 remote_env、queue、state：

```python
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute

def test_target_and_source_routes_are_persisted_before_network(remote_env, wire):
    prepared = queue(remote_env, remote_env["target"])
    dist, op, mapping = state(prepared.task_id)
    target = FrozenTikTokRoute.model_validate(dist.target_route)
    source = FrozenTikTokRoute.model_validate(dist.source_route)
    assert target.connection_id == remote_env["connection_id"]
    assert source.connection_id == remote_env["connection_id"]
    assert target.tenant_id == source.tenant_id == remote_env["context"].tenant_id
    assert op is not None and mapping is None and wire[0] == []
```

- [ ] 执行 `uv run pytest tests/modules/materials/test_remote_only_distribution.py -q`，预期新路由字段断言失败。FrozenTikTokRoute 是 P0 的 frozen Pydantic BaseModel，使用 model_validate/model_dump，不自建第二种 route DTO。
- [ ] 根集成人员在 P3 Task 2 父路由与 P1 Task 7 场景路由已合入后，于 backend 执行 `uv run alembic heads`，确认单一真实 head，再执行 `uv run alembic revision --rev-id mcp_material_routes -m "material frozen routes"`；只由这一人生成并审阅迁移，不允许多个代理并发生成或猜 down_revision。记录生成的真实父 revision，不能修改历史迁移。
- [ ] 为 MaterialDistribution 增加 `target_route`、`source_route` JSON 字段，为 MaterialAssetOperation/MaterialCoverJob 增加 `frozen_route`；新记录应用级必填并逐次验证，迁移阶段历史空值仅在可核实旧连接时回填。创建依赖时 `target_route=route.model_dump(mode='json')`；来源选择后在同事务固定 `source_route`，分别验证 read/upload 能力。各素材对象自己保存路由，不另建全局泛型上下文表。迁移核心列变更为：

```python
for table, names in (
    ("material_distribution", ("target_route", "source_route")),
    ("material_asset_operation", ("frozen_route",)),
    ("material_cover_job", ("frozen_route",)),
):
    for name in names:
        op.add_column(table, sa.Column(name, sa.JSON(), nullable=True))
```

- [ ] 在迁移测试中先建旧 API 操作，保存请求/VID/source advertiser，升级后逐值断言不变；旧连接证据完整时仅新增 route，授权版本无法证明时保留空 route 并返回 `material_route_unverified`，错误映射为 HTTP 409/“历史素材连接信息需要核实”。旧 UNKNOWN 不能读取新 BC 默认补齐。测试新增 API 恢复和拒绝恢复两条路径，迁移降级拒绝删除存在 MCP 任务的路由证据。
- [ ] 同步所有必填 route 调用点：`builds/execution.py` 的两处 ensure_target_asset、两处 ensure_cover，以及 `builds/material_execution.py` 的 ensure_cover，均从父 preview 加载原 route；相关 fixture 同步造真实路由。独立上传入口先固定 route，后台任务只读已保存值。执行 `rg -n "ensure_target_asset\(|ensure_cover\(" app tests` 逐处核对，运行 builds 的素材准备相关回归，避免阶段合入后漏参。
- [ ] 将 `_target_access`、`_work_connection` 和 `_send_relay` 的默认连接选择移除：源 GET 使用 source_route，目标 URL POST/回读使用 target_route；目标已核实映射优先，无来源权限则 blocked。新任务、重复派发、token 轮换沿原路由；授权语义变化暂停。保留一个目标素材的既有唯一身份，重复消费者不得创建第二次上传。
- [ ] 在现有“另一有效连接不覆盖真实上传连接”和并发测试中增加默认路由切换及另一租户来源用例；用真实 BC 默认绑定行切换，不能 monkeypatch resolver。执行 `uv run pytest tests/modules/materials/test_remote_only_distribution.py tests/modules/materials/test_distribution_concurrency.py tests/modules/materials/test_source_uploads.py -q`，提交 `materials: freeze target routes and source dependencies`。

## Task 3: URL 写入能力门禁与一次发送

依赖 Task 2 全部路由与调用方迁移通过；不能先启用 URL 写入再补路由。

**Files:** Create `backend/app/modules/materials/channel_policy.py`；Modify 两个 material adapter、source_url_uploads.py、source_uploads.py、readiness.py、uploads.py、core/errors.py；Test `backend/tests/modules/materials/test_material_channel_policy.py` 和 `test_url_sdk_contract.py`。

**Interfaces:** `MaterialUploadPolicy(max_bytes: int | None, identity_verified: bool, no_auto_fix_verified: bool, no_auto_bind_verified: bool, unsafe_server_retry: bool)` 为 frozen dataclass；`require_url_upload(policy: MaterialUploadPolicy, *, byte_size: int) -> None`。策略来源是 P0/P2 已记录且绑定合同版本的能力证据，不来自客户端请求或工具存在与否。

- [ ] 新增测试代码并执行 `uv run pytest tests/modules/materials/test_material_channel_policy.py -q`，预期新模块缺失：

```python
import pytest
from app.core.errors import DomainError
from app.modules.materials.channel_policy import MaterialUploadPolicy, require_url_upload

@pytest.mark.parametrize("policy", [
    MaterialUploadPolicy(None, True, True, True, False),
    MaterialUploadPolicy(1024**3, False, True, True, False),
    MaterialUploadPolicy(1024**3, True, False, True, False),
    MaterialUploadPolicy(1024**3, True, True, False, False),
    MaterialUploadPolicy(1024**3, True, True, True, True),
])
def test_unknown_contract_does_not_enable_upload(policy):
    with pytest.raises(DomainError):
        require_url_upload(policy, byte_size=1)

def test_application_capacity_does_not_override_service_evidence():
    policy = MaterialUploadPolicy(256 * 1024**2, True, True, True, False)
    require_url_upload(policy, byte_size=256 * 1024**2)
    with pytest.raises(DomainError):
        require_url_upload(policy, byte_size=1024**3)
```

- [ ] 实现先门禁后签 URL/armed 的纯判断；为新错误添加 HTTP 409 与中文文案，身份不符继续沿现有错误码：

```python
from dataclasses import dataclass
from app.core.errors import DomainError
from app.core.config import settings

@dataclass(frozen=True)
class MaterialUploadPolicy:
    max_bytes: int | None
    identity_verified: bool
    no_auto_fix_verified: bool
    no_auto_bind_verified: bool
    unsafe_server_retry: bool

def require_url_upload(policy, *, byte_size):
    if policy.max_bytes is None or not all((policy.identity_verified,
            policy.no_auto_fix_verified, policy.no_auto_bind_verified)) or policy.unsafe_server_retry:
        raise DomainError("material_channel_unverified", "当前连接的素材入库能力待核实")
    if type(byte_size) is not int or not 0 < byte_size <= min(settings.MATERIAL_URL_MAX_UPLOAD_BYTES, policy.max_bytes):
        raise DomainError("material_channel_capacity", "文件超过当前连接已核实的上传容量")
```

- [ ] 实现一次 URL upload：业务保存 expected_md5，但 MCP 只发送 P0 合同明确支持的字段；`auto_fix_enabled=False` 也需 schema 支持并按合同设置。缺 auto_bind 字段只能凭官方默认行为/可核实证据建立策略，不能凭省略字段推断。SDK 文件上传仍保留原容量保护；MCP `upload_video_file` 在网络前抛 `material_channel_unverified`，不读文件、不编码上传。
- [ ] `source_url_uploads.run_url_source_upload` 在现有 armed 提交后调用 `gateway.materials.upload_video_url(request, budget=budget)`；request 的 advertiser_id、file_name、expected_md5、byte_size 来自已保存来源操作/已核实原件代次，url 来自发送前签发的链接。立即保存 VideoReceipt 中实际 ID，再退出 gateway。请求/响应持久化白名单不包含 URL、MCP token 或本机路径；不要把 DTO `asdict()` 全量保存。
- [ ] 执行 `uv run pytest tests/modules/materials/test_material_channel_policy.py tests/modules/materials/test_url_sdk_contract.py tests/modules/materials/test_url_ingest.py -q`，保持 URL/FILE 两条 API 既有合同通过；添加 MCP transport 捕获断言，不出现未支持的 `video_signature` 键；`auto_bind_enabled`/`auto_fix_enabled` 已出现在当前官方工具声明中，只有连接观察确认支持时才显式发送 false，不能套用 SDK 字段。提交 `materials: gate and execute verified channel uploads`。

## Task 4: 图片/封面闭环与目标可用性

**Files（实际实施）:** Modify covers.py、cover_models.py、cover_sdk.py、sdk_assets.py、adapters/mcp_materials.py、adapters/sdk_materials.py；新增 `test_channel_covers.py`、`test_cover_adapters.py`，更新原封面/素材适配测试。root 集成 gateway/errors 及迁移 `mcp_cover_evidence_freeze_cover_video_digest_and_preserve_.py`、`test_cover_evidence_migration.py`。cover_tasks.py/readiness.py 的现有期限与可用性规则沿用并回归，无需改写。

**Interfaces:** `ensure_cover(session, *, context, bc_id, material_id, advertiser_id, task_key, route: FrozenTikTokRoute) -> AssetPreparation` 继承目标 route；`read_video_cover` 保留 md5 核实；`suggest_cover` 的 width/height 是筛选目标比例，返回 VideoCover 或 None；图片上传回执与详情验证分开。

实施审查补齐原要求的持久证据：新 job 保存不可变 `video_md5`，备用回执保存 `receipt_facts`，明确区分实际无签名与历史 SQL NULL。迟到签名冲突撤下对应可用图片并保留冲突围栏，普通核查不得清除；只读状态也核对原摘要和原连接当前 read 权限。迁移接实际 `mcp_draft_connection`，不回填旧事实。

- [ ] 在 test_covers 的 queue helper 固定 route 并新增测试；沿用 source_env/wire、seed/queue/job_state：

```python
def test_cover_is_frozen_to_actual_target_before_any_call(source_env, wire):
    seed(source_env)
    pending = queue(source_env)
    job = job_state(pending.task_id)
    assert job.frozen_route["connection_id"] == str(source_env["connection_id"])
    assert job.advertiser_id == "actual-account"
    assert job.video_id == "vid-actual-account"
    assert job.known_image_id is None and wire[0] == []
```

- [ ] 执行 `uv run pytest tests/modules/materials/test_covers.py -q` 观察路由断言失败；将 `_call` 改用固定 gateway，保留每步一次调用及 receipt 保存 hook。图片 URL 仅来自本目标视频详情或同比例建议封面，不以任意用户 URL 绕过现有来源校验。
- [ ] 从现有 cover_sdk 迁移实际请求到 adapter；纯 `verified_image` 与 `image_search_page` 继续验证实际 ID、尺寸、displayable、signature、文件名和完整分页。`suggest_cover` 返回 `VideoCover(url,width,height)`，找不到同比例候选返回 None；业务不得自动裁图/绑定别的 VID。
- [ ] 现有测试参数化两通道，增加上传成功但 cleanup 抛错、图片详情不可用、签名不符、搜索两候选、父 video_id 改变五种传输响应序列；每种断言最多一个图片 POST，未知不发布 image_id，清理错误不抹掉 known_image_id。
- [ ] 执行 `uv run pytest tests/modules/materials/test_covers.py tests/modules/materials/test_cover_sdk.py tests/modules/materials/test_readiness_batch.py -q`；目标 VID/封面任一不就绪时 AssetPreparation 不为 ready。提交 `materials: verify channel cover preparation`。

## Task 5: UNKNOWN、远端拉取与原件清理围栏

**Files（实际实施）:** Modify source_url_uploads.py 与 `test_url_ingest.py`；新增 `test_unknown_original_fences.py`、`test_source_upload_prefork.py`。object_uses.py、cleanup.py、cleanup_scan.py、cleanup_abandoned.py、cleanup_reconcile.py 的现有用途保护经回归成立，未做无必要改写。实际修复跨 worker 分页总行数/已见 ID 的持久核对；Linux 双 worker 用例另待目标环境执行。

**Interfaces:** 沿用 OriginalUse 的 `purpose='ingest'` 与原 `operation_id`、release_object_uses 和源操作状态。仅实际精确视频回读完成，或在途调用方可证明未发送/官方可证明无副作用终态，才结束原件用途；单独收到远端 VID 不够。

- [ ] 添加下面真实数据库测试到 test_url_ingest.py，复用已有 imports/helpers；API 的 ReadTimeoutError 与 MCP 丢响应分别在传输边界注入相同语义：

```python
def test_expired_url_and_worker_loss_keep_original_reserved(url_env, redis_client, wire):
    wire[1].append(ReadTimeoutError(None, "/upload", "offline lost response"))
    run(url_env, redis_client)
    op = operation(url_env)
    assert op.status == "result_unknown"
    with Session(engine) as db, db.begin():
        use = db.exec(select(OriginalUse).where(OriginalUse.operation_id == op.id)).one()
        use.expires_at = datetime.now(UTC) - timedelta(days=2)
        obj = db.get(TemporaryMaterialObject, url_env["object_id"])
        from app.modules.materials.cleanup import _active_uses
        assert [row.id for row in _active_uses(db, obj)] == [use.id]
        assert obj.reservation_released_at is None
        assert obj.reserved_bytes == len(CONTENT)
    assert len([call for call in wire[0] if call[0] == "POST"]) == 1
```

- [ ] 执行 `uv run pytest tests/modules/materials/test_url_ingest.py -q`；已有 API 保护可能已通过，新增 MCP 路径必须先验证缺失/失败，不能为了红灯破坏正确的旧实现。
- [ ] 复用 `_failure` 的 send_armed/post_attempted 判定，MCP 调用抛错不能等同未发送；client cleanup 异常不清空 VID。读空/多候选保持 UNKNOWN，只排只读核查；正常刷新不改路由，撤权后不另选连接。并发 worker 失效只撤销其本地 claim，不解除副作用重发围栏。
- [ ] 保持 cleanup `_active_uses` 对 ingest 不按时间过期，修复任何 scan/abandoned/reconcile 旁路；仅原子提交终态及用途释放后才调度删除。沿用字节预留直至删除回读确认；未知用途在上传进度保留错误原因。无活跃用途仍需既有删除资格证据，不能以“全部过期”替代。
- [ ] 执行 `uv run pytest tests/modules/materials/test_url_ingest.py tests/modules/materials/test_object_uses.py tests/modules/materials/test_cleanup.py tests/modules/materials/test_cleanup_scan.py tests/modules/materials/test_cleanup_abandoned.py tests/modules/materials/test_cleanup_reconcile.py -q`；覆盖两个真实 prefork worker 的迟到回执/进程退出，用现有 prefork 测试启动方式，不 mock DB 或 Redis。提交 `materials: retain originals for unresolved remote uploads`。

## 阶段收口

- [ ] 在 backend 执行 `uv run pytest tests/modules/materials tests/integrations/tiktok/test_material_contracts.py -q`、`uv run ruff check app/integrations/tiktok app/modules/materials tests/integrations/tiktok/test_material_contracts.py`、`uv run ty check`；迁移测试必须覆盖历史 API 原件、UNKNOWN、来源映射无改写与新 MCP 路由。只读命令未实际执行不得标记通过。
- [ ] 从仓库根执行 `rg -n 'sdk_client|business_api_client|official_client' backend/app/modules/materials` 核对已迁移调用；纯业务解析文件不残留 SDK import。未启用共享接口不新增 MCP share 分支；原 SDK 共享代码收口 adapter 且继续不可选，不开通未经验证的共享。
- [ ] 在 `docs/implementation-progress.md` 登记实际文件、唯一迁移 revision、测试结果及能力证据缺项；向 P3 交付 MaterialOperations 和两项 ensure_* 的最终签名。根核对全体调用方后按明确文件完成阶段提交；不自动推送或启用真实入库。
- [ ] 真实验证另开具体授权记录，核对 BC/来源和目标账户、文件摘要/大小、MCP 合同版本、真实 VID/图片映射、最大容量及上传时长。没有该证据时界面保持能力待核实，不把离线 1 GiB fixture 当作远端容量验收。
