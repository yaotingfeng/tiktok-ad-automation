# TikTok 创建即启用与后台执行 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将冻结预览可靠提交为立即启用的 Smart+ 广告，支持共享限流、租户公平调度、部分失败续建和未知结果回查。

**Architecture:** PostgreSQL 保存提交和步骤事实，事务 outbox 投递 Celery，Redis 只保存准入与速率协调状态。执行直接调用固定官方 SDK 的 CampaignCreationApi、AdgroupApi、AdApi；每次外部写入前持久化请求意图，返回后立即保存实际 ID，UNKNOWN 先查远端，不盲目重放。

**Tech Stack:** Python >=3.14、FastAPI、SQLModel、Alembic、PostgreSQL、Celery、Redis、官方 TikTok Python SDK、React/shadcn、Bun/Playwright。

**Spec:** [05 搭建执行](../specs/2026-09-08-tiktok-05-build-execution-design.md)、[04 投放策略](../specs/2026-09-08-tiktok-04-strategies-design.md)。输入契约见 [计划 05](2026-09-08-tiktok-05-strategies-preview-plan.md)。

## Global Constraints

- 应用目录 `/Users/yaotingfeng/Documents/ytf/ytf-os-ad-skill/tiktok-ad-automation`；所有代码路径相对此目录。本文件保留原实施计划，当前完成情况见 `docs/implementation-progress.md`。
- 官方 SDK 固定 Git revision `f809c396520df2d7b201a9ccc5378d822b728ed3` 的 `python_sdk` 子目录，import 为 `business_api_client`；不修改 SDK 源码，不自建 HTTP、MCP 或抽象 Gateway。
- “用户提交的动作明确为‘创建并立即启用’：三层创建请求都直接带 `ENABLE`。”
- “回读只核查实际结果，不作为开启投放的门槛，不存在第二次启用授权步骤。”
- “任一后续步骤失败时保留已成功的结构与广告，不自动回滚或关闭已开始投放的成功部分。”
- “回读仍无法确定时保持待核实，不能因超过等待时间便认定不存在并重放。”
- “Celery 负责任务调度，业务步骤使用数据库执行租约防止并发重复写入。”Redis 不存业务任务队列，PostgreSQL 不自建第二套调度器。
- 单批单租户单 BC；提交和每个待执行步骤都重新校验权限。撤权只阻止后续写入，不改变已经成功的远端状态。
- 快照中的素材内容、组、预算、ROAS、文案、名称不变；只补充目标账户素材映射和远端 ID。
- 全部有效剧目覆盖全部有效账户；排除项保留，修复后的新提交不自动重建原先已提交组合。
- 根 API 前缀 `/api/tenants/{tenant_id}/`；不做投后自动调价、经营数据同步、分析库或自动删除空结构。
- 真实 SDK 商业字段、Minis 权限和限额按 Task 2 的证据契约与计划 07 试投验证，不把 SDK 生成类存在当作平台全能力保证。
- 后端命令从 `backend/` 运行，前端命令从 `frontend/` 运行；提交步骤只供以后正式实施执行。

---

## 执行顺序与 Files

Task 2 不依赖本计划的提交/执行表：可在计划 05 Task 1～4 后先完成，再执行计划 05 Task 5～6。
共享 SDK 准入由计划 01 提供，计划 02/04 从一开始复用；Task 3 仅增加搭建业务公平调度，不形成资源计划反向依赖。

| 文件 | 职责 |
|---|---|
| `backend/app/modules/builds/{execution_models,submissions}.py` | 提交、步骤、租约、幂等和摘要 |
| `backend/app/modules/builds/{scene_context,sdk_requests}.py` | 官方场景证据、参数编译和直接 SDK 调用 |
| `backend/app/modules/builds/{execution,reconciliation,tasks}.py` | 缺失步骤执行、只读核查、Celery 入口 |
| `backend/app/modules/builds/fairness.py`、`fair_turn.lua` | 消费计划 01 准入，增加搭建租户的公平轮候元数据 |
| `backend/app/alembic/versions/0006_build_execution.py` | 提交唯一约束、步骤和证据日志 |
| `backend/tests/fakes/tiktok.py` | 在官方 ApiClient.call_api 边界注入测试远端，不另造业务 Gateway |
| `backend/tests/modules/builds/`、`backend/tests/jobs/` | SDK 序列化、事务、租约、回读与准入回归 |
| `frontend/src/features/builds/submission-api.ts`、`SubmissionProgress.tsx` | 提交动作、分层进度与部分重试 |
| `docs/contracts/tiktok-minis-fields.md` | 每个字段的官方来源、SDK 序列化与真实联调记录 |

输入：计划 02 `require_tenant/resolve_account_access/sdk_client`；计划 04 `ensure_target_asset`；计划 05 `load_frozen_unit/get_frozen_groups/get_preview_summary`。`sdk_client` 返回官方 ApiClient，token 仅在请求内从 `client.default_headers["Access-Token"]` 读取，不记录或复制到任务。
`ensure_target_asset(..., task_key: str) -> AssetPreparation(state="ready"|"queued"|"blocked", mapping, task_id, reason_code, reason_message)`；ready 的 `mapping` 是目标账户 `AccountAsset`，使用 `video_id/image_id`，不能拿源账户 ID 代替。
共享夹具：`session/context/client/redis_client`；跨进程测试使用专用测试库及真实 commit，不能把外层未提交事务当作 worker 可见数据。

### Task 1: 幂等提交、步骤表与事务 outbox

**Files:**
- Create: `backend/app/modules/builds/execution_models.py`、`submissions.py`。
- Create: `backend/app/alembic/versions/0006_build_execution.py`。
- Modify: `backend/app/modules/builds/schemas.py`。
- Test: `backend/tests/modules/builds/test_submissions.py`、`conftest.py`。

**Interfaces:**
- Consumes: 计划 05 冻结预览和 `enqueue_after_commit(session, *, context, task_name, task_key, payload) -> UUID`。
- Produces: `submit_preview(session, *, context, preview_id: UUID, request_id: UUID) -> SubmissionReceipt`；`get_submission(session, *, context, submission_id: UUID) -> SubmissionView`。
- `SubmissionReceipt(submission_id: UUID, status: str)`；`ObjectCounts(campaign_count: int, adgroup_count: int, ad_count: int)`。
- `SubmissionView` 包含 `submission_id/status/currency/daily_budget_sum: str`，以及 `planned/submitted/succeeded/failed/excluded/unknown/pending: ObjectCounts`、`stage_counts: dict[str,int]`。planned=submitted+excluded；submitted=succeeded+failed+unknown+pending。UNKNOWN 或状态差异优先 NEEDS_REVIEW。

- [ ] **Step 1: 添加重复键、不同键同预览的失败测试。** `ready_preview` 在本模块 conftest 通过计划 05 真正生成冻结预览，不直接伪造 Submission。

```python
from uuid import uuid4
from app.modules.builds.submissions import submit_preview

def test_same_preview_has_only_one_submission(session, context, ready_preview):
    first = submit_preview(session, context=context,
        preview_id=ready_preview.preview_id, request_id=uuid4())
    second = submit_preview(session, context=context,
        preview_id=ready_preview.preview_id, request_id=uuid4())
    session.flush()
    assert first.submission_id == second.submission_id
    assert ready_preview.count_submissions(session) == 1
    assert ready_preview.count_outbox_intents(session) == 1
```

- [ ] **Step 2: 红测。** `uv run pytest tests/modules/builds/test_submissions.py -q`；预期提交服务缺失。
- [ ] **Step 3: 增加明确的表和约束。** `Submission(id,tenant_id,preview_id,request_id,actor_id,bc_id,status)` 唯一 `(tenant_id,preview_id)` 和 `(tenant_id,request_id)`；同请求键指向另一个 preview 返回 `idempotency_conflict`。`ExecutionStep(id,tenant_id,submission_id,unit_id,kind,group_id,planned_ad_id,step_key,status,phase,remote_id,attempt,lease_token,lease_expires_at,request_digest,next_attempt_at)` 对步骤键唯一。`StepEvidence` 追加保存 attempt、request_id、SDK 响应摘要、获取时间和核查结论，禁止覆盖历史。
步骤 kind 为 `MATERIAL/CTA/CAMPAIGN/ADGROUP/AD/READBACK`；同租户复合外键覆盖层级，所有 ID 保持字符串或 UUID。状态字段无 activate 状态或自动回滚指令。
- [ ] **Step 4: 实现锁预览后提交的核心事务。** 外层路由统一 commit，任何校验失败连 outbox 一起 rollback。

```python
from uuid import uuid4
from sqlmodel import select
from app.core.errors import DomainError
from app.jobs.outbox import enqueue_after_commit
from app.modules.builds.models import BuildPreview
from app.modules.builds.execution_models import Submission
from app.modules.builds.schemas import SubmissionReceipt

def reserve_submission(session, *, context, preview_id, request_id):
    preview = session.exec(select(BuildPreview).where(
        BuildPreview.id == preview_id, BuildPreview.tenant_id == context.tenant_id
    ).with_for_update()).one_or_none()
    if preview is None or preview.status != "FROZEN":
        raise DomainError("preview_not_submittable", "preview_not_submittable")
    existing = session.exec(select(Submission).where(
        Submission.tenant_id == context.tenant_id,
        Submission.preview_id == preview_id)).one_or_none()
    if existing:
        return SubmissionReceipt(submission_id=existing.id, status=existing.status)
    row = Submission(id=uuid4(), tenant_id=context.tenant_id, preview_id=preview_id,
        request_id=request_id, actor_id=context.actor_id, bc_id=preview.bc_id, status="QUEUED")
    session.add(row)
    session.flush()
    enqueue_after_commit(session, context=context, task_name="builds.expand_submission",
        task_key=f"submission:{row.id}", payload={"submission_id": str(row.id)})
    return SubmissionReceipt(submission_id=row.id, status=row.status)
```

公开 `submit_preview` 先检查当前草稿 revision、预览 counts>0、操作者 build 权限和实际提交单元的账户/版权方/素材权限，再调用此事务。排除项不触发整批失败。对相同 request_id 的不同 preview 在 reserve 前查并返回冲突，数据库唯一约束处理竞态；冻结预览行必须复制 `bc_id`。分批 expansion 仅给实际提交范围创建步骤，唯一键确保重复消息不重复展开。
- [ ] **Step 5: 绿测、事务测试并提交。** 增加两个独立 DB connection 并发提交、撤权、过期预览、rollback 无 outbox 的测试；`uv run alembic upgrade head`；`uv run pytest tests/modules/builds/test_submissions.py -q`。预期同预览一条提交、一份 outbox。`git add backend/app/modules/builds backend/app/alembic/versions/0006_build_execution.py backend/tests/modules/builds`；`git commit -m "builds: submit frozen previews idempotently"`。

### Task 2: 官方 SDK 场景契约、只读资产与真实序列化

**Files:**
- Create: `backend/app/modules/builds/scene_context.py`、`sdk_requests.py`。
- Create: `backend/tests/fakes/tiktok.py`、`backend/tests/modules/builds/test_sdk_contract.py`。
- Create: `docs/contracts/tiktok-minis-fields.md`。

**Interfaces:**
- Consumes: 计划 02 `sdk_client/resolve_account_access`、计划 03 的 `link_id` 与版权方应用事实；不依赖 Submission/ExecutionStep。
- Produces: `read_scene_context(session, *, context, bc_id: str, advertiser_id: str, link_id: UUID) -> SceneContext`。
- `SceneContext` 为冻结 dataclass：`supported: bool, reason_codes: tuple[str,...], capability_revision: str, campaign_fields: dict, adgroup_fields: dict, creative_fields: dict, cta_fields: dict, name_limit: int, creative_limit: int, copy_length_limit: int, evidence_ids: tuple[UUID,...]`。这些字典是每个账户的小型已核实场景快照，不接受前端任意 JSON，不代替业务实体。
- `compile_request(kind: str, *, fixed: dict, resolved: dict) -> dict`、`invoke_create(client, *, kind: str, body: dict) -> RemoteCreated(remote_id: str, request_id: str | None, operation_status: str | None)`。
- `TikTokResponseError(DomainError)` 额外保留 `remote_code: int` 和 `request_id: str | None`，供执行层记录非零响应与返回缺 ID 的证据。
- Fake 统一入口：`FakeTikTokAPI.call_api(resource_path, http_method, *args, **kwargs)`；`store[kind][remote_id]`、`calls: list[dict]`，返回官方 `InlineResponse200`。

- [ ] **Step 1: 固定官方证据，不推测生成类。** 已查实类分别为 [CampaignCreationApi](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/python_sdk/business_api_client/api/campaign_creation_api.py)、[AdgroupApi](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/python_sdk/business_api_client/api/adgroup_api.py)、[AdApi](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/python_sdk/business_api_client/api/ad_api.py)。创建接口都接受 body 参数；官方 [ApiClient](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/python_sdk/business_api_client/api_client.py) 可直接序列化 dict。记录 SDK `SmartPlusAdgroupCreateBody` 不包含 minis_id，不能以 typed model 过滤掉文档确认的字段，也不能把 app_id 冒充 Minis。

实施此步骤读取官方开发文档中 “Get the TikTok Minis within an ad account” 和 “Create an Upgraded Smart+ TikTok Growth Max: Mini Dramas campaign”，在证据文件逐字段登记：endpoint、请求/响应层级、合法枚举、条件必填、来源 URL、SDK revision、验证状态。公开目录确认 `/minis/get/`，但不据目录标题推断所有参数；必须从该 endpoint 的完整 Request 表确定 advertiser_id、分页/筛选参数和响应列表字段，再加入测试 fixture。无当前可核实文档的组合返回 `unsupported_scene`，不把缺口改成网站广告或旧 APP_INSTALL 模式。

- [ ] **Step 2: 写 SDK 序列化红测。** 直接截获官方 client.call_api，验证未知模型字段在原生 dict 中不会丢失；`documented_extra` 是序列化测试字段，绝不发送到生产 API。

```python
import business_api_client as sdk

def test_official_method_accepts_unmodeled_dictionary(monkeypatch):
    client = sdk.ApiClient()
    captured = {}
    def capture(path, method, *args, **kwargs):
        captured.update(path=path, method=method, body=client.sanitize_for_serialization(kwargs["body"]))
        return sdk.InlineResponse200(code=0, data={"adgroup_id": "group-1"})
    monkeypatch.setattr(client, "call_api", capture)
    sdk.AdgroupApi(client).smart_plus_adgroup_create("test-token", body={
        "advertiser_id": "account-1", "operation_status": "ENABLE", "documented_extra": "kept"})
    assert captured["path"] == "/open_api/v1.3/smart_plus/adgroup/create/"
    assert captured["body"]["documented_extra"] == "kept"
```

`uv run pytest tests/modules/builds/test_sdk_contract.py -q`：首轮环境应明确暴露未安装固定 SDK 或缺少场景检查；基础序列化用例可先通过，其余新增业务用例必须先失败。
- [ ] **Step 3: 建立本地场景读取及官方 SDK 通用入口。** 用已授权账户查询 Identity 与官方 CTA/portfolio 可用选项，保留来源及更新时间，不按首个返回项静默选择。Minis 若生成方法缺失，直接使用官方 `ApiClient.call_api` 的固定官方路径；请求参数列表只由 Step 1 的字段证据构造，示例核心如下，函数入参 `query` 必须先由该场景解析器白名单验证：

```python
def sdk_minis_read(client, *, query):
    return client.call_api(
        "/open_api/v1.3/minis/get/", "GET", {}, query,
        {"Access-Token": client.default_headers["Access-Token"], "Accept": "application/json"},
        response_type="InlineResponse200", auth_settings=[],
        _return_http_data_only=True, _request_timeout=(10, 60),
    )
```

10/60 是可配置的本地连接/读取超时，不是平台限额。该入口不替代已存在的 SDK endpoint 方法，不接受任意 URL 或用户指定 HTTP method。校验应用事实、Minis ACTIVE/地区、Identity 权属及 CTA 支持，无法确定时产生明确原因；provider application_id 与 minis_id 分字段保存。将官方确认的枚举及条件字段编译进 SceneContext，预算/ROAS/名称/账户/状态不能由场景字段覆盖。将 `sdk_minis_read` 参数白名单和跨租户测试加入本文件；本阶段离线 fixture 通过不代表真实 Minis 已验收。
- [ ] **Step 4: 实现直接 SDK 创建核心，不额外包装传输。**

```python
from dataclasses import dataclass
from copy import deepcopy
import business_api_client as sdk
from app.core.errors import DomainError

@dataclass(frozen=True)
class RemoteCreated:
    remote_id: str
    request_id: str | None
    operation_status: str | None

class TikTokResponseError(DomainError):
    def __init__(self, *, code, request_id, reason):
        super().__init__(reason, reason)
        self.remote_code = code
        self.request_id = request_id

def compile_request(kind, *, fixed, resolved):
    protected = {"advertiser_id", "campaign_id", "adgroup_id", "campaign_name", "adgroup_name",
                 "ad_name", "budget", "budget_optimize_on", "roas_bid", "operation_status"}
    if protected.intersection(resolved):
        raise DomainError("scene_overrides_frozen_fields", "scene_overrides_frozen_fields")
    body = {**deepcopy(resolved), **deepcopy(fixed), "operation_status": "ENABLE"}
    if kind == "campaign":
        body["budget_optimize_on"] = True
    if kind == "adgroup" and "budget" in body:
        raise DomainError("adgroup_budget_not_allowed", "adgroup_budget_not_allowed")
    return body

def invoke_create(client, *, kind, body):
    token = client.default_headers["Access-Token"]
    methods = {
        "campaign": (sdk.CampaignCreationApi(client).smart_plus_campaign_create, "campaign_id"),
        "adgroup": (sdk.AdgroupApi(client).smart_plus_adgroup_create, "adgroup_id"),
        "ad": (sdk.AdApi(client).smart_plus_ad_create, "smart_plus_ad_id"),
    }
    method, key = methods[kind]
    if body.get("operation_status") != "ENABLE":
        raise DomainError("invalid_creation_status", "invalid_creation_status")
    response = method(token, body=body, _request_timeout=(10, 60))
    if response.code != 0:
        raise TikTokResponseError(code=response.code, request_id=response.request_id,
            reason=f"tiktok_{response.code}")
    data = response.data
    if not isinstance(data, dict) or not data.get(key):
        raise TikTokResponseError(code=response.code, request_id=response.request_id,
            reason="create_result_unknown")
    return RemoteCreated(str(data[key]), response.request_id, data.get("operation_status"))
```

实际外部错误原始码及 request_id 保存到脱敏 StepEvidence，由 Task 4/5 决定是否已知未写入；这里不自动 retry。Campaign fixed 含 `advertiser_id/campaign_name/budget`，AdGroup fixed 含 `advertiser_id/campaign_id/adgroup_name/roas_bid`，Ad fixed 含账户、父组、名称。Minis、优化事件、归因和定向使用本步骤有证据的 resolved，不在本计划臆造值。
- [ ] **Step 5: 写 FakeTikTokAPI，供计划 07 复用。** Fake 的 create/get data 形状分别遵循官方 ID 字段与分页列表；校验三层 ENABLE，不支持 status-update，确保测试意外改变成功广告时立即失败。

```python
from copy import deepcopy
import json
import business_api_client as sdk

class FakeTikTokAPI:
    def __init__(self):
        self.store = {"campaign": {}, "adgroup": {}, "ad": {}}
        self.calls = []

    def call_api(self, resource_path, http_method, *args, **kwargs):
        kind = resource_path.split("/")[-3]
        action = resource_path.split("/")[-2]
        if kind not in self.store or action not in {"create", "get"}:
            raise AssertionError(f"Unexpected SDK operation: {resource_path}")
        body = deepcopy(kwargs.get("body") or {})
        self.calls.append({"path": resource_path, "method": http_method, "body": body})
        key = {"campaign": "campaign_id", "adgroup": "adgroup_id", "ad": "smart_plus_ad_id"}[kind]
        if action == "create":
            assert http_method == "POST" and body["operation_status"] == "ENABLE"
            remote_id = str(900000 + len(self.store[kind]))
            row = {**body, key: remote_id, "operation_status": "ENABLE", "secondary_status": "AD_STATUS_AUDIT"}
            self.store[kind][remote_id] = row
            return sdk.InlineResponse200(code=0, message="OK", request_id="fake-request", data=deepcopy(row))
        assert http_method == "GET"
        query = dict(args[1] if len(args) > 1 else kwargs.get("query_params", []))
        rows = [r for r in self.store[kind].values() if r["advertiser_id"] == query["advertiser_id"]]
        filters = query.get("filtering", {})
        if isinstance(filters, str):
            filters = json.loads(filters)
        for field in ("campaign_ids", "adgroup_ids", "smart_plus_ad_ids"):
            if filters.get(field):
                singular = {"campaign_ids":"campaign_id", "adgroup_ids":"adgroup_id", "smart_plus_ad_ids":"smart_plus_ad_id"}[field]
                rows = [r for r in rows if r.get(singular) in filters[field]]
        for field in ("campaign_name", "adgroup_name"):
            if filters.get(field):
                rows = [r for r in rows if r.get(field) == filters[field]]
        page, size = int(query.get("page", 1)), int(query.get("page_size", 10))
        data = {"list": deepcopy(rows[(page-1)*size:page*size]),
                "page_info": {"page": page, "page_size": size, "total_number": len(rows), "total_page": (len(rows)+size-1)//size}}
        return sdk.InlineResponse200(code=0, message="OK", data=data, request_id="fake-read")
```

本 Fake 只覆盖广告三层；Minis/Identity/CTA fixture 在 `test_sdk_contract.py` 按已核实的各 endpoint 响应独立设置，遇到未知 endpoint 明确失败，禁止默认返回成功。计划 07 可以包装 fake.call_api 制造成功后丢响应。
- [ ] **Step 6: 绿测并提交。** `uv run pytest tests/modules/builds/test_sdk_contract.py -q`；预期三层 ENABLE、父级字段、原生 dict 序列化、Minis 缺能力阻断、Ad 返回 `smart_plus_ad_id` 均通过。`git add backend/app/modules/builds/scene_context.py backend/app/modules/builds/sdk_requests.py backend/tests/fakes/tiktok.py backend/tests/modules/builds/test_sdk_contract.py docs/contracts/tiktok-minis-fields.md`；`git commit -m "builds: bind verified Smart+ SDK contracts"`。

### Task 3: 复用全局限流与搭建租户公平调度

**Files:**
- Create: `backend/app/modules/builds/fairness.py`、`fair_turn.lua`。
- Modify: `backend/app/modules/builds/tasks.py`、计划 01 的 Celery 配置文件。
- Test: `backend/tests/modules/builds/test_fairness.py`。

**Interfaces:**
- Consumes: 计划 01 `app.jobs.admission` 的 `Admission(granted:bool,retry_after_ms:int)`、`AdmissionPolicy(app_max_inflight:int,endpoint_max_inflight:int,tenant_max_inflight:int,advertiser_max_inflight:int,app_calls_per_window:int,endpoint_calls_per_window:int,window_ms:int,lease_ms:int)`；均为正整数，无本计划重新定义字段。
- Consumes: `admit_call(redis_client, *, app_scope:str, endpoint:str, tenant_id:UUID, advertiser_id:str, lease_id:UUID, policy:AdmissionPolicy) -> Admission`；`release_call(redis_client, *, app_scope:str, endpoint:str, tenant_id:UUID, advertiser_id:str, lease_id:UUID) -> None`。
- Produces: `take_fair_turn(redis_client, *, app_scope:str, tenant_id:UUID, owner:UUID, wait_ms:int, turn_ms:int) -> bool`、`finish_fair_turn(redis_client, *, app_scope:str, tenant_id:UUID, owner:UUID, served:bool, retry_after_ms:int=0) -> None`。
- app_scope 是共享开发者应用 ID，不是租户 connection_id；所有素材和广告 SDK 调用使用同一入口。

- [ ] **Step 1: 添加真实 Redis 公平轮候失败测试。** 只测试搭建业务轮次，共享额度、窗口和租约测试由计划 01 负责。

```python
from uuid import UUID, uuid4
from app.modules.builds.fairness import take_fair_turn, finish_fair_turn

def test_waiting_tenant_gets_next_turn(redis_client):
    a, b, owner = UUID(int=1), UUID(int=2), uuid4()
    scope = f"fair-test-{uuid4().hex}"
    args = dict(app_scope=scope, wait_ms=30000, turn_ms=3000)
    assert take_fair_turn(redis_client, tenant_id=a, owner=owner, **args)
    assert not take_fair_turn(redis_client, tenant_id=b, owner=uuid4(), **args)
    finish_fair_turn(redis_client, app_scope=scope, tenant_id=a, owner=owner, served=True)
    assert not take_fair_turn(redis_client, tenant_id=a, owner=uuid4(), **args)
    assert take_fair_turn(redis_client, tenant_id=b, owner=uuid4(), **args)

def test_account_denial_does_not_block_another_tenant(redis_client):
    a, b, owner = UUID(int=3), UUID(int=4), uuid4()
    scope = f"fair-denial-{uuid4().hex}"
    args = dict(app_scope=scope, wait_ms=30000, turn_ms=3000)
    assert take_fair_turn(redis_client, tenant_id=a, owner=owner, **args)
    assert not take_fair_turn(redis_client, tenant_id=b, owner=uuid4(), **args)
    finish_fair_turn(redis_client, app_scope=scope, tenant_id=a,
        owner=owner, served=False, retry_after_ms=5000)
    assert take_fair_turn(redis_client, tenant_id=b, owner=uuid4(), **args)
```

- [ ] **Step 2: 红测。** `uv run pytest tests/modules/builds/test_fairness.py -q`；预期公平轮候模块不存在。
- [ ] **Step 3: 只保存租户轮候元数据，不自建任务队列。** Lua 的 KEYS 依次为等待租户 ZSET、轮次锁、单调序号、last_seen ZSET、not_before ZSET；候选member是tenant_id，score是登记序号。登记、过期清理、取得轮次、转到队尾和释放均在同一个Lua内原子完成。所有键使用计划01相同的app_scope hash tag。

```lua
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local mode, tenant, owner = ARGV[1], ARGV[2], ARGV[3]
local wait_ms, turn_ms = tonumber(ARGV[4]), tonumber(ARGV[5])
local token = tenant .. ':' .. owner
if mode == 'finish' then
    if redis.call('GET', KEYS[2]) ~= token then return 0 end
    redis.call('DEL', KEYS[2])
    if ARGV[6] == '1' then
        for _, i in ipairs({1, 4, 5}) do redis.call('ZREM', KEYS[i], tenant) end
    else
        redis.call('ZADD', KEYS[1], redis.call('INCR', KEYS[3]), tenant)
        redis.call('ZADD', KEYS[4], now, tenant)
        redis.call('ZADD', KEYS[5], now + tonumber(ARGV[7]), tenant)
    end
    return 1
end
local stale = redis.call('ZRANGEBYSCORE', KEYS[4], '-inf', now - wait_ms)
for _, candidate in ipairs(stale) do
    for _, i in ipairs({1, 4, 5}) do redis.call('ZREM', KEYS[i], candidate) end
end
if not redis.call('ZSCORE', KEYS[1], tenant) then
    local sequence = redis.call('INCR', KEYS[3])
    redis.call('ZADD', KEYS[1], sequence, tenant)
end
redis.call('ZADD', KEYS[4], now, tenant)
for _, i in ipairs({1, 3, 4, 5}) do redis.call('PEXPIRE', KEYS[i], wait_ms * 2) end
local first = nil
for _, candidate in ipairs(redis.call('ZRANGE', KEYS[1], 0, -1)) do
    local due = tonumber(redis.call('ZSCORE', KEYS[5], candidate) or '0')
    if due <= now then first = candidate; break end
end
if first ~= tenant then return 0 end
local locked = redis.call('SET', KEYS[2], token, 'NX', 'PX', turn_ms)
if not locked then return 0 end
return 1
```

Python包装器将mode、配置和标识传入同一register_script；finish仅能释放自己的轮次锁。served=False代表本次公平尝试已经用过，移到队尾并按retry_after_ms延后，不能占着队头阻塞其他账户或端点仍有额度的租户。基础额度被拒绝不消耗rate，但会消费一次公平尝试轮次。轮次锁只跨越基础admit调用，不跨网络请求；实际SDK调用在基础额度lease保护下运行。过期清理不在Python中先读后删。
- [ ] **Step 4: 接入基础准入与 Celery。** `worker_prefetch_multiplier=1`、`task_acks_late=True`、`task_reject_on_worker_lost=True`。先take_fair_turn，再调用计划01 admit_call；admission.granted=False则finish(served=False,retry_after_ms=admission.retry_after_ms)，以Celery retry调度；成功则finish(served=True)，执行结束finally release_call，不能返还已经消耗的rate窗口计数。数据库步骤只有获得额度后才ARMED。等待时不持有DB行锁、不长时间sleep，Redis不可用时不调用SDK。

```python
from math import ceil

def retry_seconds(retry_after_ms, jitter_seconds):
    return max(1, ceil(retry_after_ms / 1000)) + jitter_seconds
```

上式供Celery countdown使用，jitter是小范围非负调度扰动，不改变冻结广告内容。业务轮候保持租户层面公平，素材与只读资产调用继续直接使用计划01全局准入。BC/授权查询缺少advertiser_id时只在内部准入传空字符串，不发送给TikTok。
- [ ] **Step 5: 绿测并提交。** 补充A持续请求时B获下一轮、同租户并发只有一个轮次锁、A账户额度耗尽但B有额度时B继续获准、拒绝不消耗rate但A转队尾、worker退出后锁到期、过期候选原子清理、finally归还基础inflight。`uv run pytest tests/modules/builds/test_fairness.py -q`；预期公平且不突破计划01额度。`git add backend/app/modules/builds/fairness.py backend/app/modules/builds/fair_turn.lua backend/app/modules/builds/tasks.py backend/tests/modules/builds/test_fairness.py`；`git commit -m "builds: schedule tenant writes fairly through shared admission"`。

### Task 4: 素材准备、请求意图与三级 ENABLE 创建

**Files:**
- Create: `backend/app/modules/builds/execution.py`、`tasks.py`。
- Modify: `sdk_requests.py`、`execution_models.py`、`submissions.py`。
- Test: `backend/tests/modules/builds/test_execution.py`。

**Interfaces:**
- Consumes: `FrozenUnit/FrozenGroup/FrozenAd`、`ensure_target_asset`、Task 2 SDK 函数、Task 3 准入。
- Produces: `execute_step(session, *, context, step_id: UUID, lease_token: UUID) -> str`；`claim_step(session, *, context, step_id: UUID, owner: UUID, lease_seconds: int) -> UUID | None`；Celery task `builds.expand_submission`、`builds.execute_step`。

- [ ] **Step 1: 添加广告请求素材集合失败测试。** 每条 SP 只包含自己的一个文案，但 creative_list 挂该组全部目标素材。

```python
from app.modules.builds.sdk_requests import ad_assets

def test_each_sp_uses_the_same_group_and_one_text():
    mappings = [{"video_id":"target-v1", "image_id":"cover-1"},
                {"video_id":"target-v2", "image_id":"cover-2"}]
    identity = {"identity_type":"BC_AUTH_TT", "identity_id":"identity-test",
                "identity_authorized_bc_id":"bc-test"}
    a = ad_assets(mappings, text="Watch an episode.", url="https://example.test/minis", identity=identity)
    b = ad_assets(mappings, text="See where the story goes.", url="https://example.test/minis", identity=identity)
    assert a["creative_list"] == b["creative_list"]
    assert len(a["ad_text_list"]) == len(b["ad_text_list"]) == 1
    assert a["ad_text_list"] != b["ad_text_list"]
```

- [ ] **Step 2: 红测。** `uv run pytest tests/modules/builds/test_execution.py -q`；预期缺少业务请求编译/执行。
- [ ] **Step 3: 使用已核实的官方 Ad creative 形状。** 官方 [CreativeInfo](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/python_sdk/docs/SmartPlusAdCreateBodyCreativeInfo.md)、[VideoInfo](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/python_sdk/docs/SmartPlusAdCreateBodyCreativeInfoVideoInfo.md)、[ImageInfo](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/python_sdk/docs/SmartPlusAdCreateBodyCreativeInfoImageInfo.md) 确认字段；官方 [Ad 请求示例](https://business-api.tiktok.com/gateway/docs/index?doc_id=1855007163018689) 确认 SINGLE_VIDEO、Identity 与文案/链接列表结构，该示例的网站投放参数不迁移为 Minis 设置。

```python
from app.core.errors import DomainError

def ad_assets(mappings, *, text, url, identity):
    creatives = []
    for item in mappings:
        if not item.get("video_id") or not item.get("image_id"):
            raise DomainError("target_asset_incomplete", "target_asset_incomplete")
        creatives.append({"creative_info": {
            "ad_format": "SINGLE_VIDEO", "video_info": {"video_id": item["video_id"]},
            "image_info": [{"web_uri": item["image_id"]}], **identity}})
    if not creatives:
        raise DomainError("empty_material_group", "empty_material_group")
    return {"creative_list": creatives, "ad_text_list": [{"ad_text": text}],
            "landing_page_url_list": [{"landing_page_url": url}]}
```

- [ ] **Step 4: 实现每一步的持久化边界。** claim 使用状态条件 UPDATE，生成新 lease_token、attempt，phase=CLAIMED；没有名额前不置 REQUEST_ARMED。先校验 actor/tenant/BC/账户 build 权限，再分发材料；ensure queued 时保存依赖并通过 Celery 等待，blocked 标该组失败，其他无依赖单元继续。全部目标 video/image 回查完成后保存映射，CTA 若场景要求 portfolio 则创建独立可恢复步骤。对冻结物料原文件上传的合法替代路径不改内部 material_id。

Campaign→AdGroup→Ad 依赖按本地实际远端 ID 判断，不等待创建后回读才开启子层；每个 create 直接 ENABLE。请求完整 body 在调用前保存 digest 与受保护字段，phase=REQUEST_ARMED 并 commit，随后调用 SDK；不能在网络请求期间持有 DB transaction。以下是持久化状态转换的核心：

```python
from sqlalchemy import update
from app.core.errors import DomainError
from app.modules.builds.execution_models import ExecutionStep

def arm_request(session, *, step_id, tenant_id, lease_token, request_digest):
    changed = session.execute(update(ExecutionStep).where(
        ExecutionStep.id == step_id, ExecutionStep.tenant_id == tenant_id,
        ExecutionStep.lease_token == lease_token, ExecutionStep.status == "RUNNING",
        ExecutionStep.phase == "CLAIMED"
    ).values(phase="REQUEST_ARMED", request_digest=request_digest))
    if changed.rowcount != 1:
        raise DomainError("execution_lease_lost", "execution_lease_lost")
    session.commit()
```

响应成功先追加 StepEvidence，再条件更新 remote_id/SUCCEEDED，事务提交后 outbox 触发子步骤；旧 lease 的迟到结果只追加证据并排队核查，不覆盖新 owner 状态。请求序列化异常发生在 arm 前可确定失败；arm 后任何 transport timeout/断连/返回缺 ID 进入 UNKNOWN。SDK 非零码仅在官方明确保证未创建的错误类别里设 FAILED/RETRYABLE，否则也 UNKNOWN。禁止 broad `autoretry_for=(Exception,)`。
- [ ] **Step 5: 绿测并提交。** 测试三层 body ENABLE、组无独立 budget、相同素材组 SP 正文不同、TARGET VID替换source VID、源撤权但原文件恢复、CTA 重试复用、任一失败不调用 status/delete、每一步 remote_id 立即落库。`uv run pytest tests/modules/builds/test_execution.py -q`；预期全部通过。`git add backend/app/modules/builds backend/tests/modules/builds/test_execution.py`；`git commit -m "builds: create enabled Smart+ entities step by step"`。

### Task 5: UNKNOWN、丢 ACK 与只读核查

**Files:**
- Create: `backend/app/modules/builds/reconciliation.py`。
- Modify: `execution.py`、`tasks.py`、`execution_models.py`。
- Test: `backend/tests/modules/builds/test_reconciliation.py`。

**Interfaces:**
- Consumes: SDK 三层 get、步骤请求快照和 StepEvidence。
- Produces: `reconcile_step(session, *, context, step_id: UUID) -> str`；`match_remote(*, name_field: str, expected_name: str, expected_parent: tuple[str,str] | None, rows: list[dict], complete: bool) -> tuple[str, dict | None]`；Celery `builds.reconcile_step`。

- [ ] **Step 1: 写“远端成功但响应丢失”和空页不能证明未创建的失败测试。**

```python
from app.modules.builds.reconciliation import match_remote

def test_exact_match_and_empty_readback_are_distinct():
    row = {"ad_name":"batch-g01-sp1", "adgroup_id":"group-1", "smart_plus_ad_id":"ad-1"}
    state, found = match_remote(name_field="ad_name", expected_name="batch-g01-sp1",
        expected_parent=("adgroup_id","group-1"), rows=[row], complete=True)
    assert state == "FOUND" and found["smart_plus_ad_id"] == "ad-1"
    assert match_remote(name_field="ad_name", expected_name="batch-g01-sp1",
        expected_parent=("adgroup_id","group-1"), rows=[], complete=True)[0] == "UNKNOWN"
```

- [ ] **Step 2: 红测。** `uv run pytest tests/modules/builds/test_reconciliation.py -q`，预期核查模块不存在。
- [ ] **Step 3: 完整读取目标账户与父级范围，匹配精确名称。** Campaign/AdGroup get filtering 支持各自名称；Ad get filtering 没有 ad_name，必须按父 AdGroup/已有 ID 分页，再本地精确比较，禁止编造过滤参数。使用标准 `adgroup_get` 补读 Smart+ get 缺少的 operation_status，字段按固定 SDK 及官方契约选择。核心匹配：

```python
def match_remote(*, name_field, expected_name, expected_parent, rows, complete):
    matches = []
    for row in rows:
        if row.get(name_field) != expected_name:
            continue
        if expected_parent and row.get(expected_parent[0]) != expected_parent[1]:
            continue
        matches.append(row)
    if complete and len(matches) == 1:
        return "FOUND", matches[0]
    return "UNKNOWN", None
```

匹配后还验证账户、父级、预算/ROAS、链接、创意文案与映射后的素材集合；只有同一实际对象才补存 ID，不仅比较对象总数。多匹配、分页不完整、读取无权限、空列表、参数不一致都进入 NEEDS_REVIEW。读取后发现非 ENABLE 只记差异，不调用 status 修改。成功 get 不代表审核通过或已消耗。
- [ ] **Step 4: 编写过期 lease 和结果未知恢复。** Celery 重投递看到 SUCCEEDED 立即返回；CLAIMED且没有ARMED证据的过期步骤可重新领取；REQUEST_ARMED或已记录外部开始的过期步骤只能转 UNKNOWN 后 enqueue reconcile。原网络调用可能仍在运行时，恢复 worker 禁止 create；迟到结果作为追加证据处理。只有官方确定未写入响应，或经过明确证据确认不存在且原尝试不再可能落库，才能回到可执行；一次空查或时间流逝不能构成该证据。

```python
def expired_step_action(*, status, phase, has_remote_id):
    if status == "SUCCEEDED" or has_remote_id:
        return "READBACK"
    if phase == "REQUEST_ARMED":
        return "RECONCILE"
    return "RECLAIM"
```

增加断点故障注入：fake先写store再抛TimeoutError；执行后 PostgreSQL unknown，随后fake get返回该对象，核查补ID且create调用数仍为1。模拟SDK成功后DB提交失败、任务ACK丢失、旧worker迟到响应、Ad位于第2页；各测试检查没有重复create或任何status/delete。
- [ ] **Step 5: 绿测并提交。** `uv run pytest tests/modules/builds/test_reconciliation.py -q`；预期每个不确定分支保留证据，不自动重建。`git add backend/app/modules/builds/reconciliation.py backend/app/modules/builds/execution.py backend/app/modules/builds/tasks.py backend/tests/modules/builds/test_reconciliation.py`；`git commit -m "builds: reconcile unknown writes before retrying"`。

### Task 6: 部分重试、精确摘要与任务列表/详情 UI

本 Task 的 UI-04/UI-12 布局与交互为本轮新增待评审草案，依据设计第 12 节；已有提交、立即启用、状态和恢复规则继续有效。只补页面计划，不在本轮实现应用。

**Files:**
- Modify: `backend/app/modules/builds/submissions.py`、`api.py`、`schemas.py`。
- Create: `frontend/src/features/builds/submission-api.ts`、`SubmissionListPage.tsx`、`SubmissionDetailPage.tsx`。
- Create: `frontend/src/features/builds/SubmissionProgress.tsx`、`SubmissionRecoveryActions.tsx`、`SubmissionUnitsTable.tsx`、`SubmissionIssueSheet.tsx`。
- Create: `frontend/src/routes/_layout/tenants.$tenantId.build-tasks.tsx`（父路由只渲染 Outlet）、`tenants.$tenantId.build-tasks.index.tsx`、`tenants.$tenantId.build-tasks.$submissionId.tsx`（薄路由，只连接页面和上下文，列表与详情不叠加）。
- Modify: `frontend/src/features/builds/BuildPreviewPanel.tsx` 与搭建页容器。
- Test: `backend/tests/modules/builds/test_progress.py`、`test_submission_queries.py`、`frontend/tests/build-execution.spec.ts`、`build-task-pages.spec.ts`。

**Interfaces:**
- Consumes: 计划 05 PreviewSummary、Task 1 SubmissionView。
- Produces: `retry_failed_steps(session, *, context, submission_id: UUID) -> int`；`request_reconciliation(session, *, context, submission_id: UUID) -> int`；POST submit/retry/reconcile、GET submission及分页步骤接口。
- submit JSON `{request_id: UUID}` 返回 `{submission_id,status}`；retry/reconcile 返回 `{scheduled_count:int}`。重复同 request_id 必须使用原 preview；前端生成一次后保留至取得 submission_id。
- UI-04 消费 `GET /api/tenants/{tenant_id}/submissions`，参数 `bc_id,q,status_group,created_from,created_to,provider_connection_id,cursor,limit`；`status_group` 为 all/active/attention/completed，时间参数为含时区 ISO 时间；页面显式传 `limit=50`，可切换为 100，不依赖接口默认值。返回 `Page[SubmissionListItem]`，不伪造总页数。
- `SubmissionListItem` 包含 submission_id/batch_short_id/status、created_at/updated_at、actor_name、provider_name/strategy_label、drama_count/account_count/excluded_unit_count，以及 submitted/succeeded/failed/unknown 的 ObjectCounts。账户 ID 完整匹配，其他搜索文本字面包含；所有查询固定 tenant+BC，按 created_at/id 倒序稳定分页。
- UI-12 的 `SubmissionView` 增补 preview_id/batch_short_id/bc_id、created_at/updated_at、actor_name/provider_name/strategy_label、drama_count/account_count/excluded_unit_count，保留 Task 1 所有计数字段。增补 `recovery`：retryable_step_count/reconcilable_step_count 为 int，can_retry/can_reconcile 为 bool，reasons 为 list[str]；它表示当前操作者对现有服务的可操作结果，不引入新恢复路径。
- `GET /submissions/{submission_id}/units` 返回账户×剧目分页；`GET /submissions/{submission_id}/units/{unit_id}/groups` 返回组及其当前页 Ad；`GET /submissions/{submission_id}/steps` 支持账户/剧目/kind/result 筛选；`GET /submissions/{submission_id}/excluded` 与 `/events` 分别分页返回排除组合与脱敏操作记录。均复用 Page，页面不下载完整任务后过滤。
- 前端路由固定为 `/tenants/:tenantId/build-tasks?bc_id=…`（UI-04）、`/tenants/:tenantId/build-tasks/:submissionId`（UI-12）；实际 TanStack 路由文件使用 `$tenantId` 与 `$submissionId`。UI-12 的数据范围取任务自身绑定；新建入口为 `/tenants/:tenantId/builds/new`。

- [ ] **Step 1: 添加 UNKNOWN 优先于 PARTIAL 的失败测试。**

```python
from app.modules.builds.submissions import aggregate_status

def test_unknown_takes_precedence_and_success_is_kept():
    assert aggregate_status(["SUCCEEDED","FAILED","UNKNOWN"]) == "NEEDS_REVIEW"
    assert aggregate_status(["SUCCEEDED","FAILED"]) == "PARTIAL"
    assert aggregate_status(["SUCCEEDED","SUCCEEDED"]) == "COMPLETED"
    assert aggregate_status(["FAILED","FAILED"]) == "FAILED"
```

- [ ] **Step 2: 红测。** `uv run pytest tests/modules/builds/test_progress.py -q`；预期摘要行为缺失。
- [ ] **Step 3: 实现摘要优先级与计数。**

```python
def aggregate_status(states):
    values = set(states)
    if "UNKNOWN" in values or "MISMATCH" in values:
        return "NEEDS_REVIEW"
    if values.intersection({"PENDING","RUNNING","RETRYABLE"}):
        return "RUNNING"
    if values == {"SUCCEEDED"}:
        return "COMPLETED"
    if "SUCCEEDED" in values and "FAILED" in values:
        return "PARTIAL"
    if values == {"FAILED"}:
        return "FAILED"
    return "QUEUED"
```

以冻结计划对象为计数全集，排除项计入planned/excluded，CTA/MATERIAL/READBACK不混入三级广告数。父步骤确定失败导致永远不执行的子对象记录明确 dependency_failed，进入failed；仍在排队的计pending。已建对象即使回读参数差异仍保存成功创建事实，其核查状态单独使整体 NEEDS_REVIEW，不能重复计入success与unknown。返回每层 operation_status/审核状态附加信息，unknown指是否创建未知，不等于未审核。摘要不访问报表 API。
- [ ] **Step 4: 实现只恢复缺失步骤的 API。** retry 重新鉴权，只调度FAILED/RETRYABLE且依赖已满足、原快照仍有效的步骤，UNKNOWN只走reconcile；没有可靠结果不得被“重试全部”绕过。SUCCEEDED全部跳过。需要改素材、预算、文案的修复返回 `new_preview_required`，新批次不能自动带入已提交组合。GET支持按账户/剧目/阶段分页，tenant越界统一404。recovery 由同一候选步骤查询生成，不从前端失败标签估算；其数量单位为步骤，不能当作 Ad 数量。

```typescript
import { BuildsService } from "@/client";

export async function submitFrozenPreview(tenantId: string, previewId: string, requestId: string) {
  const response = await BuildsService.submitPreview({
    path: { tenant_id: tenantId, preview_id: previewId },
    body: { request_id: requestId },
  });
  return response.data;
}
```

后端路由固定tags=["builds"]、operation_id="builds-submit_preview"和路径参数preview_id，重新生成客户端得到BuildsService.submitPreview；沿用模板client.gen的Bearer认证配置，不使用credentials-only fetch。新增读取和恢复路由的 operation_id 分别为 builds-list_submissions/get_submission/get_submission_units/get_submission_groups/get_submission_steps/get_submission_excluded/get_submission_events/retry_submission/reconcile_submission，使生成的 BuildsService 方法名确定。提交中禁用按钮；网络失败重试同requestId，取得ID后进入 UI-12。

- [ ] **Step 5: 先写任务页面失败回归。** 新增 `build-task-pages.spec.ts`，在已有登录测试上下文内注册确定的 API 响应。以下 fixture 保持原始三级计数守恒，模拟 1 Campaign/1 Ad Group/2 Ads，其中 SP1 已创建、SP2 创建结果未知；不是用前端布尔值代替执行事实。

```typescript
import { test, expect } from "@playwright/test";

test("未知 Ad 先核查，已有结果保持可见且没有激活入口", async ({ page }) => {
  const tenantId = "00000000-0000-4000-8000-000000000001";
  const submissionId = "00000000-0000-4000-8000-000000000002";
  const root = `/api/tenants/${tenantId}/submissions/${submissionId}`;
  const counts = (campaign: number, group: number, ad: number) => ({
    campaign_count: campaign, adgroup_count: group, ad_count: ad,
  });
  let reconciliations = 0;
  let retries = 0;
  await page.route(`**${root}/reconcile`, async (route) => {
    reconciliations += 1;
    await route.fulfill({ json: { scheduled_count: 1 } });
  });
  await page.route(`**${root}/retry`, async (route) => {
    retries += 1;
    await route.fulfill({ json: { scheduled_count: 0 } });
  });
  await page.route(`**${root}/units*`, async (route) => {
    await route.fulfill({ json: { items: [], next_cursor: null } });
  });
  await page.route(`**${root}`, async (route) => {
    await route.fulfill({ json: {
      submission_id: submissionId,
      preview_id: "00000000-0000-4000-8000-000000000003",
      batch_short_id: "B08001", bc_id: "bc-test", status: "NEEDS_REVIEW",
      currency: "USD", daily_budget_sum: "100.00",
      actor_name: "测试运营", provider_name: "网眼", strategy_label: "默认策略 v1",
      created_at: "2026-09-08T01:00:00Z", updated_at: "2026-09-08T01:01:00Z",
      drama_count: 1, account_count: 1, excluded_unit_count: 0,
      planned: counts(1, 1, 2), submitted: counts(1, 1, 2),
      succeeded: counts(1, 1, 1), failed: counts(0, 0, 0),
      unknown: counts(0, 0, 1), pending: counts(0, 0, 0), excluded: counts(0, 0, 0),
      stage_counts: {}, recovery: {
        retryable_step_count: 0, reconcilable_step_count: 1,
        can_retry: false, can_reconcile: true, reasons: [],
      },
    } });
  });
  await page.goto(`/tenants/${tenantId}/build-tasks/${submissionId}`);
  await expect(page.getByRole("heading", { name: "任务 B08001" })).toBeVisible();
  await expect(page.getByTestId("count-ad-succeeded")).toHaveText("1");
  await expect(page.getByTestId("count-ad-unknown")).toHaveText("1");
  await expect(page.getByRole("button", { name: /重试失败/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /激活|再次启用|停止广告/ })).toHaveCount(0);
  await page.getByRole("button", { name: "核查待核实项（1）" }).click();
  await expect(page.getByRole("status")).toContainText("已安排 1 个步骤");
  expect(reconciliations).toBe(1);
  expect(retries).toBe(0);
  await expect(page.getByTestId("count-ad-succeeded")).toHaveText("1");
});
```

测试启动数据创建同 ID 的测试租户与 BC，由统一 auth setup 登录；只拦截任务页面业务请求，其余租户上下文读取沿用真实测试 API。运行 `bunx playwright test tests/build-task-pages.spec.ts`，新页面未实现时应因路由/可访问元素缺失而失败。后端 `test_submission_queries.py` 同时添加跨租户/跨 BC、日期边界、重复时间稳定分页和筛选改变后游标拒绝的失败测试。

- [ ] **Step 6: 实现 UI-04 列表与服务端分页。** 使用 shadcn Table/Badge/Input/Select/Popover/Button/Skeleton；沿用全站浅色紧凑布局、七项左导航和租户+BC顶栏；导航名称与权限使用全局定义。实现设计第12节的八列，默认 50 行、可选 100，列表只提供查看详情，不加多选或跨任务恢复操作；新建入口仅对有 `build` 权限者显示，空态同样校验。日期筛选按界面展示时区转换为含时区起止值，结束边界使用下一日开始前的半开区间。
状态快捷筛选映射 active=QUEUED/RUNNING、attention=PARTIAL/FAILED/NEEDS_REVIEW、completed=COMPLETED；排除组合使用独立计数。React Query key 包含 tenantId/bcId/完整筛选/cursor，切上下文取消旧请求并清理旧结果。前端保留游标栈支持上一页，URL恢复筛选；后台新增任务只提示刷新，不能在翻页中插入当前页。空列表、筛选无结果、加载和刷新失败按设计分别处理。

- [ ] **Step 7: 实现 UI-12 详情与恢复动作。** SubmissionDetailPage 只装配页头、元信息、计数表、动作区和页签；SubmissionProgress 负责三层计数及独立素材/创建/核查阶段；SubmissionUnitsTable 按页加载组合与组；SubmissionIssueSheet 用业务语言呈现原因和修复路径。成功对象操作状态、审核/投放状态和最后核查时间独立显示，不依据 ENABLE 推断已消耗。
详情页签固定搭建明细/异常与待核实/排除项/操作记录；各明细表显式请求 50 行、允许切换 100，改变筛选或页长重置对应游标。从列表异常链接进入时只改变初始页签和筛选。已提交预览只读打开 UI-11，不能再提交；排除项不显示当前任务重试按钮。恢复动作不提供逐行多选、整批重建、主动取消任务、停止成功广告或激活入口。无操作权限隐藏对应动作并展示原因；权限失效保留用户仍有读取权限的结果。
恢复操作的关键组件只接受服务端计算的可处理范围，不自行把 UNKNOWN 放进 retry；UI 状态不改变执行意图：

```tsx
import { Button } from "@/components/ui/button";

type Recovery = {
  retryable_step_count: number; reconcilable_step_count: number;
  can_retry: boolean; can_reconcile: boolean; reasons: string[];
};

export function SubmissionRecoveryActions({ recovery, busy, onRetry, onReconcile }: {
  recovery: Recovery; busy: boolean;
  onRetry: () => void; onReconcile: () => void;
}) {
  return <div className="flex flex-wrap items-center gap-2" aria-label="任务恢复操作">
    {recovery.reconcilable_step_count > 0 && recovery.can_reconcile && <Button
      disabled={busy} onClick={onReconcile}>
      核查待核实项（{recovery.reconcilable_step_count}）
    </Button>}
    {recovery.retryable_step_count > 0 && recovery.can_retry && <Button variant="outline"
      disabled={busy} onClick={onRetry}>
      重试失败步骤（{recovery.retryable_step_count}）
    </Button>}
    {recovery.reasons.length > 0 && <p className="text-sm text-muted-foreground">
      {recovery.reasons.join("；")}
    </p>}
  </div>;
}
```

实施时 Recovery 类型取生成 API DTO 的 recovery 字段，避免手写类型长期重复。按钮直接调用同任务的 BuildsService.retrySubmission/reconcileSubmission；请求期间统一 busy，返回 scheduled_count 通过 role=status 告知实际调度量，再使摘要/明细/事件查询失效。读取失败保留已加载数据并提示更新时间；操作请求失败提示可重试读取/操作，不模拟本地成功。进行中详情可按 5 秒刷新摘要、15 秒刷新列表，这是可调整的本地 UI 节奏，不是平台请求频率；窗口失焦暂停刷新，详情回查 TikTok 只能由明确的核查操作或既有后台流程发起。

- [ ] **Step 8: 完成回归并提交。** Playwright 覆盖 submit 先断连后同键成功且仅一个submission；SP2失败后重试而SP1原ID/文案保留；UNKNOWN只核查；失败与未知并存时两动作隔离；已完成含排除时各单位清楚；权限撤销展示暂停原因且隐藏无权动作；viewer 不显示新建和无权恢复按钮；401 与 403 分别恢复登录和显示无权访问；列表及明细默认请求 50、切换后请求 100，筛选/分页/后退恢复及切租户不闪现旧结果。创建结果差异场景验证对象计已创建、任务待核实，不能重复计入unknown。
`uv run pytest tests/modules/builds/test_progress.py tests/modules/builds/test_submission_queries.py -q`；`bunx playwright test tests/build-execution.spec.ts tests/build-task-pages.spec.ts`；`bun run build`。预期通过真实摘要和页面行为断言，不把仅有 DOM 截图当成功。`git add backend/app/modules/builds frontend/src/features/builds frontend/src/routes frontend/tests/build-execution.spec.ts frontend/tests/build-task-pages.spec.ts backend/tests/modules/builds/test_progress.py backend/tests/modules/builds/test_submission_queries.py`；`git commit -m "builds: expose task results and targeted recovery"`。

### Task 7: 两 worker 与故障回归，交付集成验收契约

**Files:**
- Create: `backend/tests/modules/builds/test_worker_recovery.py`。
- Modify: `backend/tests/modules/builds/conftest.py`、`backend/tests/fakes/tiktok.py`。
- Create: `docs/testing/build-execution-fixtures.md`。

**Interfaces:**
- Consumes: 实际 SQLModel 模型、Celery 注册任务、Redis 准入、FakeTikTokAPI。
- Produces: `ready_preview` fixture 返回 `PreviewFixture(preview_id:UUID, context:TenantContext, unit_ids:tuple[UUID,...])`；辅助方法 `count_submissions(session)->int`、`count_outbox_intents(session)->int` 仅是测试方法。`sdk_fake` 为 FakeTikTokAPI，计划 07 包装其call_api注入故障。

- [ ] **Step 1: 写会捕获重复调用的真实数据库测试。** 每个worker独立Session，fixture数据先提交到专用测试库；patch官方ApiClient.call_api到同一测试远端。

```python
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

def test_one_step_is_created_once_under_two_workers(worker_harness, sdk_fake):
    step_id = worker_harness.pending_campaign_step()
    with ThreadPoolExecutor(2) as executor:
        list(executor.map(lambda owner: worker_harness.run_step(step_id, owner),
                          (uuid4(), uuid4())))
    writes = [call for call in sdk_fake.calls if call["path"].endswith("/campaign/create/")]
    assert len(writes) == 1
    assert worker_harness.step(step_id).remote_id is not None
```

- [ ] **Step 2: 红测并定义测试 harness 的明确实现。** `worker_harness` 位于本模块conftest，只调用真正的 submit/expand/claim/execute/reconcile服务，持有测试engine和context，不代替业务状态机。`pending_campaign_step()` 从ready_preview提交并完成假素材前置步骤后查询真实step ID；`run_step(step_id,owner)` 新开Session先claim再execute；`step(id)` 新开Session读状态。`uv run pytest tests/modules/builds/test_worker_recovery.py -q`，在未正确使用租约或outbox前必须暴露竞态。
- [ ] **Step 3: 增加完整故障矩阵与精确断言。** 用Task2 Fake store作为远端事实源，分别注入：create成功后Timeout、成功后DB写失败、CeleryACK丢失、worker在ARMED后退出、分页到第2页才能找到Ad、同名多对象、父创建失败但其他单元成功、SDK限流、tenant撤权、Redis短暂不可用。每个case断言创建次数、保留ID、原文案、未知状态和summary守恒，不用“没有抛错”当成功。

```python
def assert_count_conservation(view):
    for field in ("campaign_count", "adgroup_count", "ad_count"):
        assert getattr(view.planned, field) == getattr(view.submitted, field) + getattr(view.excluded, field)
        assert getattr(view.submitted, field) == sum(getattr(getattr(view, key), field)
            for key in ("succeeded", "failed", "unknown", "pending"))
```

冻结预览fixture采用2剧×3账户、每剧23份素材、每组10、N=2，正常计数6/18/36；一个组合不支持则submitted5/15/30、excluded1/3/6。素材真实内部ID/source映射由计划04测试fixture创建，不使用生产token、账户或Minis ID。SDK fake的store字段与官方data保持一致；发生意外SDK endpoint调用立即失败。
- [ ] **Step 4: 运行模块验收并交接计划 07。** `uv run pytest tests/modules/builds tests/jobs/test_admission.py -q`；`bunx playwright test tests/build-preview.spec.ts tests/build-execution.spec.ts`；`bun run build`。预期离线功能通过，不发起任何真实投放。文档记录 fixture作用域、真实commit要求、API返回和fake边界。计划07负责跨模块整体流程、容量与单次已授权真实试投，不新增第二次activate。
- [ ] **Step 5: 提交本 Task。** `git add backend/tests/modules/builds backend/tests/fakes/tiktok.py docs/testing/build-execution-fixtures.md`；`git commit -m "builds: verify worker recovery and result accounting"`。

## 自检与交接

- [ ] 每个真实写接口直接ENABLE，代码和路由无先DISABLE/再activate分支；成功对象从不被故障处理自动停用或删除。
- [ ] submit幂等、Celery重投递、执行lease、SDK远端结果未知分别有独立测试，不用名称唯一或request_id假设代替本地状态。
- [ ] 预览、提交、执行三个阶段的权限和状态定义与计划02/04/05一致；准备路径支持合法源或保存原文件，不改变素材内容。
- [ ] 数据库按account×drama和步骤持久化，异步任务只带内部ID，凭据与巨大完整批次JSON不进入Celery。
- [ ] 真实Minis字段契约和商业权限由Task2证据与计划07验收封闭；离线假数据通过不表述为真实投放已成功。
