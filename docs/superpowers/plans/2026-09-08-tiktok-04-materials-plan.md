# 素材上传、检索与账户分发 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 按用户偏好按需使用技能；本轮仅编写计划。

**Goal:** 实现本地批量上传、实际上传账户记录、按完整剧名检索素材，以及提交后向目标账户分发并确认真实 VID/MID。

**Architecture:** 原始文件存入租户隔离的对象存储，PostgreSQL 分别保存文件身份和账户资产。素材目录不绑定剧目；搭建预览只读检查可准备路径，提交后的后台任务通过官方 SDK 上传、共享与回查。

**Tech Stack:** Python 3.14、FastAPI、SQLModel、Alembic、PostgreSQL、Celery、S3 兼容对象存储及 boto3；官方 TikTok Python SDK；React、TypeScript、shadcn/ui、Bun、Playwright。

**Spec:** [整体设计](../specs/2026-09-08-tiktok-00-overall-design.md)、[素材设计](../specs/2026-09-08-tiktok-03-materials-design.md)。依赖工程基础计划 01、租户账户计划 02；可与版权方计划 03 并行。

迁移交付顺序固定为计划 03 的任务 1 后接本计划任务 1，`0004_materials.py` 的 down_revision 指向 `0003_provider_links`，保持 Alembic 单头；两模块的业务实现与测试编写仍可并行。

## Global Constraints

- 新应用根目录为 `/Users/yaotingfeng/Documents/ytf/tiktok-ad-automation`，下文路径均相对它；本轮不创建应用目录。
- 用户本地批量上传素材，系统保存文件名和文件信息，上传时不识别或绑定所属剧目。
- 上传界面固定提示：**请在素材文件名中包含完整剧目名称。广告搭建时，系统会根据剧名自动匹配素材。**
- 用户无需配置素材账户；系统从当前租户、当前 BC 的授权且具备上传权限的账户中指定一个。
- 每次上传记录实际上传账户和远端素材标识，不假设租户永久只有一个素材账户。
- 匹配是“素材文件名包含本次完整剧名”的字面包含查询，仅忽略大小写和剧名首尾空白，不解析版权方、日期或批次格式。
- 一条素材可以命中多部输入剧目，系统不据此建立永久归属，也不自动从其他剧目的预览中移除。
- 文件名升序排列，同名用 material_id 作为稳定次序。组与 SP 配置只保存在搭建快照。
- 同一远端广告账户被多个租户授权时标记归属冲突，管理员处理前不得供第二个租户上传或搭建。
- 原始文件及账户映射首版不自动过期、不开放物理删除；对象存储不得配置删除完整原文件的生命周期规则。
- TikTok SDK 固定官方仓库 revision `f809c396520df2d7b201a9ccc5378d822b728ed3` 的 `python_sdk`；导入名 `business_api_client`，不改 SDK、不写 HTTP 替代层。
- API 前缀 `/api/tenants/{tenant_id}/materials`；读取动作 `read`、用户上传 `upload`、提交后的搭建分发 `build`。
- 真实 SDK 调用依赖基础计划 01 的共享准入任务；不使用仅按单 Worker 的限流，不以 mock 准入运行生产任务。
- pytest 在新应用 `backend/` 执行；Bun 在 `frontend/` 执行。本计划的测试结果均为待执行的预期。

## 文件分工（Files）

| 文件 | 职责 |
| --- | --- |
| `backend/app/modules/materials/models.py`、`schemas.py` | 文件、对象上传会话、实际尝试、账户资产、分发任务和 DTO |
| `backend/app/modules/materials/repository.py`、`matching.py` | 租户限定目录查询、字面包含、稳定游标 |
| `backend/app/modules/materials/storage.py`、`uploads.py` | S3 分片上传、完成校验、原文件读取和平台上传入队 |
| `backend/app/modules/materials/sdk_assets.py` | 官方 FileApi/CreativeManagementApi 的业务调用及结果归类 |
| `backend/app/modules/materials/readiness.py`、`distribution.py` | 只读可准备性、真实分发、回查及恢复 |
| `backend/app/modules/materials/service.py`、`tasks.py`、`router.py` | 固定模块接口、Celery 工作单元与 HTTP 路由 |
| `frontend/src/features/materials/` | 上传队列、目录、进度和账户详情 |
| `frontend/src/routes/_layout/tenants.$tenantId.materials.tsx` | 素材工作区路由 |

## 跨计划接口（Interfaces）

消费 `TenantContext(tenant_id: UUID, actor_id: UUID, role: str)`、`DomainError(code, message, retryable=False)`、`Page[T](items, next_cursor)`、`enqueue_after_commit(session, *, context, task_name, task_key, payload) -> UUID`。
消费 `app/modules/accounts/access.py`：

- `resolve_account_access(session, *, context, bc_id: str, advertiser_id: str, action: str) -> AccountAccess`。
- `assign_upload_account(session, *, context, bc_id: str) -> AccountAccess`。
- `AccountAccess` 包含 `advertiser_id/bc_id/connection_id/currency/timezone`；连接 ID 为 UUID，远端账户和 BC ID 为字符串。

消费 `app/integrations/tiktok/sdk.py` 的 `sdk_client(session, *, context, connection_id) -> ContextManager[ApiClient]`；只在 with 范围内读取 `client.default_headers["Access-Token"]` 传入官方方法，不自行读取或保存 TikTok 凭据。
消费 `app/jobs/admission.py` 的 `admit_call(redis_client, *, app_scope: str, endpoint: str, tenant_id: UUID, advertiser_id: str, lease_id: UUID, policy: AdmissionPolicy) -> Admission` 与 `release_call(redis_client, *, app_scope: str, endpoint: str, tenant_id: UUID, advertiser_id: str, lease_id: UUID) -> None`。app_scope 为平台共用 TikTok 开发者应用 ID，不是单租户 connection_id；AdmissionPolicy/Admission 都从此模块导入。
`service.py` 导出：

```text
match_materials(session, *, context, bc_id: str, title: str,
                cursor: str | None = None) -> Page[MaterialCandidate]
get_material_readiness(session, *, context, bc_id: str, material_id: UUID,
                       advertiser_id: str) -> MaterialReadiness
ensure_target_asset(session, *, context, bc_id: str, material_id: UUID,
                    advertiser_id: str, task_key: str) -> AssetPreparation
```

`match_materials` 必须循环 next_cursor 才是完整候选集合；`get_material_readiness` 只读取本地目录、权限和对象完成状态，不调用 SDK、不创建任务。`ensure_target_asset` 只在搭建提交后调用，返回 ready、queued 或 blocked；ready 必须是目标账户已验证映射。

---

### 任务 1：建立无剧目绑定的素材目录、账户资产和分页检索

**文件（Files）：**

- 创建：`backend/app/modules/materials/models.py`、`schemas.py`、`repository.py`、`matching.py`、`service.py`。
- 创建：`backend/app/alembic/versions/0004_materials.py`。
- 测试：`backend/tests/modules/materials/conftest.py`、`test_matching.py`、`test_tenant_materials.py`。

**接口（Interfaces）：** 输出下列 DTO 与 `match_materials`；消费测试 `session/context/client`，本模块 conftest 创建 `other_context` 和关联真实租户数据。

| SQLModel 表 | 字段与约束 |
| --- | --- |
| MaterialFile | UUID、tenant_id、bc_id、file_name、file_name_folded、object_key、byte_size、sha256、video_md5、mime_type、duration、width、height、storage_state、created_at；无 drama_id |
| UploadBatch | UUID、tenant_id、bc_id、actor_id、request_id、status；唯一 tenant_id＋request_id |
| ObjectUpload | UUID、tenant_id、material_id、batch_id、s3_upload_id、object_key、expected_size、parts JSONB、status；对象 key 由服务端产生 |
| MaterialUploadAttempt | UUID、tenant_id、material_id、bc_id、advertiser_id、connection_id、operation_id、status、request_digest、remote_response JSONB；永久保留实际上传账户 |
| AccountMaterial | UUID、tenant_id、material_id、bc_id、advertiser_id、connection_id、video_id、mid、image_id、cover_url、status、verified_at；唯一 tenant_id＋material_id＋advertiser_id |
| MaterialDistribution | UUID、tenant_id、material_id、bc_id、advertiser_id、source_asset_id、operation_id、path、status、reason_code；唯一 tenant_id＋material_id＋advertiser_id 的未完成任务 |
| MaterialAssetOperation | UUID、tenant_id、material_id、advertiser_id、path、status、attempt_token、request_digest、remote_response JSONB；唯一 tenant_id＋material_id＋advertiser_id 的未核实操作，源上传与目标分发共同使用 |

- [x] **步骤 1：先写字面包含、特殊字符和无永久归属回归。**

```python
from app.modules.materials.matching import filename_matches
from app.modules.materials.models import MaterialFile

def test_complete_title_is_literal_and_case_insensitive():
    assert filename_matches("20260908_MOON 100%_01.mp4", " Moon 100% ")
    assert not filename_matches("Moon 1000_01.mp4", "Moon 100%")
    assert not filename_matches("Moonlight_01.mp4", "Moon Light")
    assert filename_matches("Moon-Sun_01.mp4", "Moon")
    assert filename_matches("Moon-Sun_01.mp4", "Sun")

def test_material_model_has_no_drama_ownership():
    assert "drama_id" not in MaterialFile.model_fields
```

- [x] **步骤 2：运行失败测试。** `uv run pytest tests/modules/materials/test_matching.py -q`；预期缺少目录模型与匹配函数而失败。

- [x] **步骤 3：实现 DTO、匹配、数据库约束和分页。**

```python
from datetime import datetime
from typing import Literal
from uuid import UUID
from pydantic import BaseModel, Field

class AccountAsset(BaseModel):
    asset_id: UUID
    material_id: UUID
    bc_id: str
    advertiser_id: str
    connection_id: UUID
    video_id: str
    mid: str | None = None
    image_id: str | None = None
    cover_url: str | None = None
    status: Literal["available", "unavailable", "result_unknown"]
    verified_at: datetime | None = None

class MaterialCandidate(BaseModel):
    material_id: UUID
    file_name: str
    bc_id: str
    original_available: bool
    source_assets: list[AccountAsset] = Field(default_factory=list)

class MaterialReadiness(BaseModel):
    state: Literal["ready", "preparable", "blocked"]
    path: Literal["existing_target", "share_source", "upload_original", "unavailable"]
    mapping: AccountAsset | None = None
    reason_code: str | None = None
    reason_message: str | None = None

class AssetPreparation(BaseModel):
    state: Literal["ready", "queued", "blocked"]
    mapping: AccountAsset | None = None
    task_id: UUID | None = None
    reason_code: str | None = None
    reason_message: str | None = None

def filename_matches(file_name: str, title: str) -> bool:
    needle = title.strip().casefold()
    return bool(needle) and needle in file_name.casefold()
```

写入时保存原始 file_name，并计算 file_name_folded；查询采用 `MaterialFile.file_name_folded.contains(title.strip().casefold(), autoescape=True)`，确保 `%/_/反斜杠` 是普通字符。空剧名返回 `empty_title`，不得匹配整个租户目录。
查询条件必含 tenant_id、bc_id，并排除尚未完整接收的文件；按 `file_name COLLATE "C" ASC, id ASC` 键集分页，每页 100 条多取一条。游标含租户、BC、标题摘要、文件名及 ID；调用时校验作用域。原始文件完整在库或已有可用账户资产时均可以返回候选。
迁移创建 tenant_id/bc_id 查询索引、稳定排序索引和 `pg_trgm` 包含检索索引；不能一次加载全部素材再在 Python 过滤。同名不同内容分别保留；同哈希新文件名保留独立 MaterialFile，原对象去重只限同租户且不删原文件名入口。

- [x] **步骤 4：验证迁移、检索和分页。** `uv run alembic upgrade head`；`uv run pytest tests/modules/materials/test_matching.py tests/modules/materials/test_tenant_materials.py -q`。预期 205 个命中完整读取无丢失/重复，其他租户和 BC 不混入，特殊字符匹配与纯函数一致，缺省不建立剧目关系。
- [x] **步骤 5：提交本任务。** `git add app/modules/materials app/alembic/versions/0004_materials.py tests/modules/materials`，然后 `git commit -m "materials: add tenant library and literal title search"`。

### 任务 2：交付对象存储分片上传、进度和事务入队

**文件（Files）：**

- 创建：`backend/app/modules/materials/storage.py`、`uploads.py`、`router.py`。
- 修改：`backend/app/api/main.py`、`backend/app/core/config.py`、`backend/pyproject.toml`、`backend/uv.lock`。
- 测试：`backend/tests/modules/materials/test_object_uploads.py`、`test_upload_api.py`。

**接口（Interfaces）：** `start_upload_batch(session, *, context, bc_id: str, files: list[UploadFileRequest], request_id: UUID) -> UploadBatchResult`；`finish_upload(session, *, context, material_id: UUID, parts: list[UploadedPart]) -> UUID` 返回平台上传任务 ID。UploadFileRequest 为 file_name、size、mime_type；UploadedPart 为 part_number、etag。浏览器不能指定广告账户或 object_key。

在 `schemas.py` 定义 `UploadFileRequest(file_name: str, size: int, mime_type: str)`、`UploadedPart(part_number: int, etag: str)`、`UploadFileResult(material_id: UUID, upload_id: UUID, part_size: int, part_count: int, status: str)`、`UploadBatchResult(batch_id: UUID, files: list[UploadFileResult])`。upload_id 是本地 ObjectUpload ID；分片默认 16 MiB，超过 10,000 片时提高分片大小，最后一片允许不足一片。size 必须大于 0，part_number 范围为 1～10,000，etag 不得为空。

- [ ] **步骤 1：先写对象 key、完整性与跨租户回归。**

```python
from uuid import uuid4
import pytest
from app.core.errors import DomainError
from app.modules.materials.storage import object_key_for, verify_object_size

def test_object_identity_never_uses_user_filename_as_path():
    tenant, material = uuid4(), uuid4()
    assert object_key_for(tenant, material) == f"tenants/{tenant}/materials/{material}/original"

def test_incomplete_file_does_not_enter_platform_upload():
    with pytest.raises(DomainError) as error:
        verify_object_size(expected=100, actual=90)
    assert error.value.code == "incomplete_object"
```

- [ ] **步骤 2：运行失败测试。** `uv run pytest tests/modules/materials/test_object_uploads.py -q`；预期缺少对象存储功能而失败。

- [ ] **步骤 3：实现有租户范围的分片上传。** 复用 P01 锁定的 boto3 依赖和配置 `S3_BUCKET/S3_ENDPOINT_URL/S3_REGION`，访问凭据使用 `S3_ACCESS_KEY_ID/S3_SECRET_ACCESS_KEY` 运行配置，不能进入前端。对象存储 bucket 私有，上传授权只对当前 ObjectUpload 的 key、upload_id 和 part_number 签名，签名有效期 900 秒；续签前重新校验租户归属。

```python
from uuid import UUID
from app.core.errors import DomainError

def object_key_for(tenant_id: UUID, material_id: UUID) -> str:
    return f"tenants/{tenant_id}/materials/{material_id}/original"

def verify_object_size(*, expected: int, actual: int) -> None:
    if expected <= 0 or actual != expected:
        raise DomainError("incomplete_object", "已接收文件大小与声明不一致")

def sign_part(s3, *, bucket: str, key: str, upload_id: str,
              part_number: int) -> str:
    if part_number < 1 or part_number > 10000:
        raise DomainError("invalid_part", "分片序号超出对象存储允许范围")
    return s3.generate_presigned_url(
        "upload_part", Params={"Bucket": bucket, "Key": key,
                               "UploadId": upload_id, "PartNumber": part_number},
        ExpiresIn=900,
    )
```

`start_upload_batch` 先校验上传动作及 BC 归属，保存每个 MaterialFile/ObjectUpload，使用 `create_multipart_upload` 建立会话。签名生成前确认调用方有权操作该行，不能仅凭对象 ID 签名。前端每片保存 etag；后端校验序号唯一且排序后调用 `complete_multipart_upload`，随后 `head_object` 核对长度；Multipart ETag 不能当文件 MD5。
`finish_upload` 完成后在同一数据库事务把文件标为 stored，并调用 `enqueue_after_commit(session, context=context, task_name="materials.upload_original", task_key=f"upload-original:{material_id}", payload={"material_id": str(material_id)})`。相同完成请求复用既有任务；对象完成成功而数据库提交失败时，通过已保存唯一 key 回查完成对象后恢复入队，不重新接收视频。
注册 `POST /api/tenants/{tenant_id}/materials/upload-batches`、`POST /api/tenants/{tenant_id}/materials/{material_id}/upload-parts/{part_number}/sign`、`POST /api/tenants/{tenant_id}/materials/{material_id}/complete`、`GET /api/tenants/{tenant_id}/materials/upload-batches/{batch_id}`。进度明确分为 receiving、stored、uploading、verifying、available、blocked 或 result_unknown；浏览器关闭后只有 stored 及以后由后台继续。

- [ ] **步骤 4：测试对象存储与事务边界。** 用 `botocore.stub.Stubber` 验证请求 key/upload_id/part_number，或基础测试环境 S3 兼容实例运行同一组集成测试；执行 `uv run pytest tests/modules/materials/test_object_uploads.py tests/modules/materials/test_upload_api.py -q`。预期错误长度不入队，跨租户无法签名/完成，同一完成请求一份 outbox，数据库回滚不发布 Celery。
- [ ] **步骤 5：提交本任务。** `git add app/modules/materials app/api/main.py app/core/config.py pyproject.toml uv.lock tests/modules/materials`，然后 `git commit -m "materials: persist resumable object uploads"`。

### 任务 3：用官方 SDK 上传并核实实际来源账户资产

**文件（Files）：**

- 创建：`backend/app/modules/materials/sdk_assets.py`、`tasks.py`。
- 修改：`backend/app/modules/materials/uploads.py`、`storage.py`。
- 创建：`docs/integrations/tiktok-materials-contract.md`。
- 测试：`backend/tests/modules/materials/test_sdk_assets.py`、`test_source_uploads.py`。

**接口（Interfaces）：** 消费 `assign_upload_account`、`resolve_account_access`、`sdk_client`；输出 `upload_video(client, *, advertiser_id: str, local_path: str, remote_name: str, md5: str)`、`read_video(client, *, advertiser_id: str, video_id: str)` 和 `run_source_upload(session, *, context, material_id: UUID) -> None`。返回的原始 SDK 响应由本任务归一化，保存真实 video_id/mid，不暴露 token。

- [ ] **步骤 1：核对固定 SDK 并写请求层回归。** 读取锁定安装包中的 `FileApi.ad_video_upload/ad_video_info/ad_video_search`、`CreativeManagementApi.creative_asset_share` 和 `AssetShareBody/FilteringVideoAdSearch`，将方法参数、响应字段及权限差异记入契约文档。已查阅的[官方 FileApi](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/python_sdk/business_api_client/api/file_api.py)包含上传与回查方法；实现继续以该 revision 安装代码为准。

```python
from unittest.mock import Mock
from app.modules.materials.sdk_assets import upload_video

def test_upload_passes_actual_account_to_official_sdk(monkeypatch):
    api = Mock()
    factory = Mock(return_value=api)
    monkeypatch.setattr("app.modules.materials.sdk_assets.FileApi", factory)
    client = Mock(default_headers={"Access-Token": "test-only-token"})
    upload_video(client, advertiser_id="target-A", local_path="/tmp/video.mp4",
                 remote_name="material-1.mp4", md5="0" * 32)
    kwargs = api.ad_video_upload.call_args.kwargs
    assert kwargs["advertiser_id"] == "target-A"
    assert kwargs["upload_type"] == "UPLOAD_BY_FILE"
    assert kwargs["video_file"] == "/tmp/video.mp4"
    assert kwargs["access_token"] == "test-only-token"
```

- [ ] **步骤 2：运行失败测试。** `uv run pytest tests/modules/materials/test_sdk_assets.py -q`；预期缺少官方 SDK 业务调用函数而失败。

- [ ] **步骤 3：实现直接 SDK 调用与流式原文件读取。**

```python
from business_api_client.api.file_api import FileApi

def upload_video(client, *, advertiser_id: str, local_path: str,
                 remote_name: str, md5: str):
    return FileApi(client).ad_video_upload(
        access_token=client.default_headers["Access-Token"],
        advertiser_id=advertiser_id, upload_type="UPLOAD_BY_FILE",
        video_file=local_path, file_name=remote_name, video_signature=md5,
        _request_timeout=(10, 300),
    )

def read_video(client, *, advertiser_id: str, video_id: str):
    return FileApi(client).ad_video_info(
        advertiser_id=advertiser_id, video_ids=[video_id],
        access_token=client.default_headers["Access-Token"],
        _request_timeout=(10, 30),
    )
```

Worker 先加载该租户 stored 文件，系统选择具备上传权限的实际账户；在 MaterialUploadAttempt 中保存账户、连接、请求摘要并关联唯一 MaterialAssetOperation 后提交，再调用 SDK。`repository.py` 提供 `reserve_asset_operation(session, *, context, material_id: UUID, advertiser_id: str, path: str) -> MaterialAssetOperation`，用唯一约束复用同文件同账户的未核实操作。外部发送认领与 attempt_token 仅存于该共用操作行。成功后先落上传响应和实际 VID/MID，再回读该账户资产，只有确认存在及状态可用才标 available。
从对象存储流式写入工作目录临时文件，分块计算 SHA-256 与 TikTok 上传所需 MD5，禁止将整个视频读入 Python 内存；平台请求结束后清理临时文件，不删除对象存储原文件。remote_name 使用内部 material_id 加扩展名以便关联，用户原始 file_name 单独保留用于检索。
每次 SDK 调用在独立 `with sdk_client(session, context=context, connection_id=account.connection_id) as client` 范围内执行；调用前统一取得共享准入，结束后 finally 释放。准入拒绝时根据 retry_after_ms 重新调度，尚不把外部步骤改成 sending；准入租约长度必须大于该调用最大超时及处理余量。无法选择账户时文件保持 stored，尝试标 blocked 并显示原因。重新选账户产生新尝试，不改写已有成功尝试的 advertiser_id；请求超时先进入 result_unknown，按已知 ID 或回查证据恢复，不马上换账户重复上传。

在 `sdk_assets.py` 提供以下调用范围。任务 3、4 的所有上传、共享和回查都进入该范围，单元测试可以 Mock 准入返回值；生产 Worker 使用基础计划真实 Redis 实现。

```python
from contextlib import contextmanager
from uuid import uuid4
import logging
from redis.exceptions import RedisError
from app.core.errors import DomainError
from app.jobs.admission import admit_call, release_call

class SdkAdmissionDeferred(Exception):
    def __init__(self, retry_after_ms: int):
        self.retry_after_ms = retry_after_ms
        super().__init__("SDK 调用等待共享配额")

@contextmanager
def admitted_asset_call(redis_client, *, app_scope, endpoint, context,
                        advertiser_id, policy):
    lease_id = uuid4()
    grant = admit_call(redis_client, app_scope=app_scope, endpoint=endpoint,
                       tenant_id=context.tenant_id, advertiser_id=advertiser_id,
                       lease_id=lease_id, policy=policy)
    if not grant.granted:
        raise SdkAdmissionDeferred(grant.retry_after_ms)
    try:
        yield
    finally:
        try:
            release_call(redis_client, app_scope=app_scope, endpoint=endpoint,
                         tenant_id=context.tenant_id, advertiser_id=advertiser_id,
                         lease_id=lease_id)
        except (RedisError, DomainError):
            logging.getLogger(__name__).warning("admission_release_failed", extra={
                "tenant_id": str(context.tenant_id), "lease_id": str(lease_id)
            })
```

用 Fake 官方 SDK 响应同时覆盖“上传成功但回查暂未出现”“目标返回不同 VID/MID”“实际账号权限失效”。真实授权就绪后用一个允许上传的短视频核实响应结构和可用性字段；只有完成此步骤才记录真实上传验收通过。

- [ ] **步骤 4：验证来源追踪与 SDK 行为。** `uv run pytest tests/modules/materials/test_sdk_assets.py tests/modules/materials/test_source_uploads.py -q`。预期两次系统选中不同来源账户时两份历史均准确；重复投递不发第二次上传；SDK 返回成功但不可用时不是 available；日志不含 token。
- [ ] **步骤 5：提交本任务。** `git add app/modules/materials tests/modules/materials ../docs/integrations/tiktok-materials-contract.md`，然后 `git commit -m "materials: upload through official SDK with source tracking"`。

### 任务 4：区分预览只读检查与提交后的可靠分发

**文件（Files）：**

- 创建：`backend/app/modules/materials/readiness.py`、`distribution.py`。
- 修改：`backend/app/modules/materials/service.py`、`tasks.py`、`sdk_assets.py`。
- 测试：`backend/tests/modules/materials/test_readiness.py`、`test_distribution.py`、`test_distribution_concurrency.py`。

**接口（Interfaces）：** 输出 `get_material_readiness` 与 `ensure_target_asset`，使用任务 1 DTO。`get_material_readiness` 只看本地已知状态；映射过期需回查时返回 preparable/existing_target，不能宣称当前远端一定可用。`ensure_target_asset` 返回的 queued.task_id 是持久化 MaterialDistribution ID。

- [ ] **步骤 1：先写原文件退路和预览无外部写测试。**

```python
from app.modules.materials.readiness import choose_material_path

def test_original_file_keeps_material_preparable_after_source_access_loss():
    result = choose_material_path(target_verified=False, target_known=False,
                                  shareable_source=False, original_available=True)
    assert result == ("preparable", "upload_original")

def test_no_source_no_original_is_explicitly_blocked():
    assert choose_material_path(target_verified=False, target_known=False,
                                shareable_source=False, original_available=False) == \
        ("blocked", "unavailable")
```

- [ ] **步骤 2：运行失败测试。** `uv run pytest tests/modules/materials/test_readiness.py -q`；预期尚无只读准备路径而失败。

- [ ] **步骤 3：实现固定路径选择、权限检查与分发入队。**

```python
def choose_material_path(*, target_verified: bool, target_known: bool,
                         shareable_source: bool,
                         original_available: bool) -> tuple[str, str]:
    if target_verified:
        return "ready", "existing_target"
    if target_known:
        return "preparable", "existing_target"
    if shareable_source:
        return "preparable", "share_source"
    if original_available:
        return "preparable", "upload_original"
    return "blocked", "unavailable"
```

`get_material_readiness` 按租户与 BC 读取素材，调用账户目录权限校验，检查目标映射、仍获授权且有 MID 的源资产及完整原文件。shareable_source 只有在 SDK 原生共享契约已核实、源目标有合法关系和权限时为 True；权限未知时不假定共享成功，有原文件则仍返回 upload_original。
`ensure_target_asset` 再次校验 build 权限和实际账户归属；ready 返回映射，blocked 返回明确原因，其余原子获取或创建唯一未完成分发任务并调用 outbox。调用传入的 task_key 对应本次搭建步骤，另以 tenant_id＋material_id＋advertiser_id 约束共享执行，两个批次可等待同一分发结果。若目标恰是正在上传的源账户，分发任务关联已有 MaterialAssetOperation 等待结果，不因来源上传和搭建分发是两种任务而重复上传。
禁止在 `get_material_readiness` 中调用 `ensure_target_asset` 或 SDK。API 不开放浏览器任意目标分发入口；搭建提交后由内部服务调用。

- [ ] **步骤 4：实现原生分享、原文件上传与目标账户回查。** [官方 AssetShareBody](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/python_sdk/business_api_client/models/asset_share_body.py)区分 material_ids 与 advertiser_ids，不能把源 VID 当作 MID；缺少有效 MID 时使用原文件上传路径。

```python
from business_api_client.api.creative_management_api import CreativeManagementApi
from business_api_client.api.file_api import FileApi
from business_api_client.models.asset_share_body import AssetShareBody
from business_api_client.models.filtering_video_ad_search import FilteringVideoAdSearch

def share_video(client, *, source_advertiser_id: str, source_mid: str,
                target_advertiser_id: str):
    body = AssetShareBody(advertiser_id=source_advertiser_id,
                          asset_type="VIDEO", material_ids=[source_mid],
                          shared_advertiser_ids=[target_advertiser_id])
    return CreativeManagementApi(client).creative_asset_share(
        access_token=client.default_headers["Access-Token"], body=body,
        _request_timeout=(10, 30),
    )

def search_shared_video(client, *, advertiser_id: str, mid: str, page: int):
    return FileApi(client).ad_video_search(
        advertiser_id=advertiser_id,
        access_token=client.default_headers["Access-Token"],
        filtering=FilteringVideoAdSearch(material_ids=[mid]),
        page=page, page_size=100, _request_timeout=(10, 30),
    )
```

共享后按目标账户查询并核实实际视频 ID；搜索过滤能否从源 MID 定位目标素材纳入真实 SDK 契约测试，未核实不能认为该方法必然返回相同 ID。共享响应直接返回的目标映射也必须目标账户回查。若缺少可靠的原生映射查询能力，使用已核实的原文件上传路径；已经发送但结果未知的共享不能立即改为重新上传，应先保留结果核实状态。
分发每个外部写步骤在 MaterialAssetOperation 上采用 PostgreSQL 条件更新认领、先持久化 sending 再调用、按 attempt_token 保存结果；同一任务的 repeated delivery 不重复发送。只有明确的共享不支持或权限拒绝且确认未成功时，才切原文件上传；断网与超时进入 result_unknown。源文件可用时源账户失权不阻塞恢复，目标账户失权仍阻塞该组合。
`AccountAsset.image_id/cover_url` 不能从源账户直接抄到目标；只记录目标回查结果，必要封面补齐由搭建执行根据官方广告参数调用素材能力完成。已 ready 的视频映射不等于任意广告参数均已校验。

- [ ] **步骤 5：验证并发、映射和只读边界。** `uv run pytest tests/modules/materials/test_readiness.py tests/modules/materials/test_distribution.py tests/modules/materials/test_distribution_concurrency.py -q`；mock 断言预览阶段 SDK 和 outbox 调用数均为零；两批并发只产生一个未完成分发；源上传与目标为同账户的分发同时到达也只发送一次；目标 VID 不同仍使用目标 ID；源权限丢失但原文件完整可继续；超时不盲目改路径重发。
- [ ] **步骤 6：提交本任务。** `git add app/modules/materials tests/modules/materials`，然后 `git commit -m "materials: separate readiness from resumable distribution"`。

### 任务 5：交付批量上传、素材目录和账户进度界面

**文件（Files）：**

- 创建：`frontend/src/features/materials/UploadPanel.tsx`、`BatchUploadSheet.tsx`、`UploadQueue.tsx`、`MaterialTable.tsx`、`AssetDetails.tsx`、`queries.ts`。
- 创建：`frontend/src/routes/_layout/tenants.$tenantId.materials.tsx`、`frontend/tests/materials.spec.ts`。
- 修改：生成客户端 `frontend/src/client/` 与导航注册文件；后端 `router.py` 增加目录及详情 GET 路由。

**接口（Interfaces）：** 消费任务 2 上传接口和任务 1 素材目录；上传队列显示 receiving/stored/uploading/verifying/available/blocked/result_unknown。查询键包含 tenant_id/bc_id/batch_id，切租户清除旧查询，所有远端账户 ID 以字符串显示。

- [ ] **步骤 1：先写上传提示与无需配置账户的页面回归。** 测试使用模板已有登录状态，并通过 page.route 返回本租户素材与批次 fixtures，禁止连接真实 TikTok。

```typescript
import { test, expect } from "@playwright/test"

test.beforeEach(async ({ page }) => {
  await page.route("**/api/tenants/*/materials?**", async (route) => {
    await route.fulfill({ json: { items: [], next_cursor: null } })
  })
})

test("批量上传不要求剧目或素材账户配置", async ({ page }) => {
  await page.goto("/tenants/1fe39141-fd11-4077-8aeb-4956ec460651/materials")
  await page.getByRole("button", { name: "批量上传", exact: true }).click()
  await expect(page.getByRole("dialog", { name: "批量上传素材" })).toBeVisible()
  await expect(page.getByText(
    "请在素材文件名中包含完整剧目名称。广告搭建时，系统会根据剧名自动匹配素材。",
  )).toBeVisible()
  await expect(page.getByLabel("选择本地素材")).toHaveAttribute("multiple", "")
  await expect(page.getByLabel("素材账户")).toHaveCount(0)
  await expect(page.getByLabel("所属剧目")).toHaveCount(0)
})
```

运行 `bunx playwright test tests/materials.spec.ts`，预期页面与命名提示不存在而失败。

- [ ] **步骤 2：实现批量文件入口。** UploadPanel 放入 BatchUploadSheet，onFiles 只更新本次待选文件，不立即创建上传；用户点击 Sheet 底部“开始上传 N 个文件”才提交批次。Sheet 具有标题、命名说明和可访问关闭按钮，页签切换不销毁公共上传管理器。

```tsx
import { Input } from "@/components/ui/input"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"

export function UploadPanel({ onFiles }: { onFiles: (files: File[]) => void }) {
  return <section aria-label="批量上传素材">
    <Field>
      <FieldLabel htmlFor="material-files">选择本地素材</FieldLabel>
      <FieldDescription>请在素材文件名中包含完整剧目名称。广告搭建时，系统会根据剧名自动匹配素材。</FieldDescription>
      <Input id="material-files" type="file" accept="video/*" multiple
        onChange={(event) => onFiles(Array.from(event.target.files ?? []))} />
    </Field>
  </section>
}
```

UploadQueue 为每个文件保存浏览器传输进度与服务端阶段；前端分片直接发送到签名 URL，使用有限并发，完成调用后轮询持久化批次。失败仅重试该文件未完成部分，已经 available 的记录不重新上传。刷新页面可恢复服务器进度，但没有完整传入的本地文件需用户重新提供原文件；不显示为后台自动继续。
MaterialTable 使用 TanStack 服务端分页、原始文件名与上传日期筛选；AssetDetails 展示每次实际账户、VID/MID、路径及回查状态。对 stored/blocked/result_unknown 使用准确中文文案，不把“文件已接收”显示成“广告账户可用”。

- [ ] **步骤 3：注册页面、生成客户端并补目录端点。** 后端 `GET /api/tenants/{tenant_id}/materials?bc_id=&cursor=&query=` 查询目录；`GET /api/tenants/{tenant_id}/materials/{material_id}` 查文件详情及可分页账户映射；`GET /api/tenants/{tenant_id}/materials/upload-batches/{batch_id}` 返回逐文件进度。上传修改要求 upload，查看要求 read；接口不包含账户选择参数和剧目绑定字段。
按基础计划的 OpenAPI 生成命令更新客户端，`queries.ts` 消费生成接口。对象存储签名 URL 只用于传输，不写入客户端持久缓存；错误详情不包含凭据或完整签名 URL。

#### 前端交互补充草案，待本轮评审：UI-05

本补充属于任务 5，按[素材设计第 10 节](../specs/2026-09-08-tiktok-03-materials-design.md)和[全局前端设计](../specs/2026-09-08-tiktok-06-frontend-experience-design.md)实现；不新增任务编号或另一套素材页面。

**文件与接口：** `BatchUploadSheet.tsx` 负责待选文件和显式开始上传；`UploadQueue.tsx` 在独立页签展示持久化批次；`MaterialTable.tsx` 负责服务端筛选分页；`AssetDetails.tsx` 展示实际账户只读记录。页面路由固定 `/tenants/:tenantId/materials`，URL 使用 `bc_id`、`tab=library|uploads`、`batch_id` 保存上下文。目录默认 50 行，可切 100；内部 `match_materials` 的 100 条分页契约保持不变。

- [ ] **UI-M1：实现表格和两页签布局。** 使用全局 Sidebar/顶栏；目录表格列为素材文件、文件信息、平台入库状态、上传账户（最近一次）、可用账户数、上传时间、操作。目录与队列分别保持筛选和游标；不在切页签时重新提交上传。
- [ ] **UI-M2：实现命名提示与上传状态分离。** Sheet 只负责文件选择和“开始上传”；成功建立批次后进入上传队列，实际文件传输由公共管理器继续。提示逐字采用设计原文，上传时不解析剧名。后台进度未返回百分比时只显示阶段，不能模拟数字进度。
- [ ] **UI-M3：实现结果未知、恢复与真实账户记录。** 账户列没有 Select/Input；尚未分配显示说明，发生账户变更时详情保留各次记录。仅 `can_retry=true`、明确失败且当前角色有 `upload` 权限的行显示重试；result_unknown 显示“查看核实进度”，批量重试必须排除它。

以下队列操作组件的输入由生成客户端结果适配，`status` 不在浏览器自行推测。`canRetry` 为服务端步骤 `can_retry` 与当前租户 `upload` 权限的交集；仅步骤可重试不能使 viewer 获得按钮。尚无字段时在任务 2 的批次进度输出中补入 `can_retry`，未知结果始终为 false。

```tsx
import { Button } from "@/components/ui/button"

type UploadRowActionsProps = {
  status: string
  canRetry: boolean
  onRetry: () => void
  onInspect: () => void
}

export function UploadRowActions({ status, canRetry, onRetry, onInspect }: UploadRowActionsProps) {
  if (status === "result_unknown") {
    return <Button variant="outline" size="sm" onClick={onInspect}>查看核实进度</Button>
  }
  if (canRetry) {
    return <Button variant="outline" size="sm" onClick={onRetry}>重试</Button>
  }
  return <Button variant="ghost" size="sm" onClick={onInspect}>查看记录</Button>
}
```

- [ ] **UI-M4：覆盖状态与权限。** 按设计状态表加入 Skeleton、空库 Empty、筛选为空、查询失败 Alert、无上传账户、权限失效和 viewer 只读；无 `upload` 权限时隐藏上传与重试按钮。401 返回登录，403 保持登录显示无权访问或无权动作；仍有读取权限时可继续查看结果。切换租户后查询缓存分离，已经创建的上传保持原始租户/BC，不重定向。
- [ ] **UI-M5：补齐浏览器验收。** 在 `frontend/tests/materials.spec.ts` 增加下表场景；使用同文件 `page.route` 返回固定状态，不调用真实 SDK。断言名称与 UI 文案固定，执行现有步骤 4 的 Playwright 命令。

| 设计验收 | 浏览器断言与固定输入 |
| --- | --- |
| UI-M01 | 打开“批量上传”后定位 dialog“批量上传素材”；提示原文完整；不存在“素材账户”combobox 或“所属剧目”字段 |
| UI-M02 | 返回一个已建立且 stored 的批次，关闭 Sheet 后点击“上传队列”，仍显示原文件；目录/队列切换期间 POST 批次次数保持 1 |
| UI-M03 | 两条历史记录使用不同 advertiser_id，详情同时显示两值；账户详情中不存在可编辑账户控件 |
| UI-M04 | 同一队列包含 retryable_error 与 result_unknown，只有前者存在“重试”；“重试明确失败项”发出的 ID 列表不含未知项 |
| UI-M05 | 分别返回空列表、500、401、403 和 viewer 上下文；对应 Empty、Alert、登录恢复、权限错误与只读状态。即使某行 can_retry=true，viewer 仍没有上传和重试按钮；不将错误误显示为零素材 |
| UI-M06 | 第一页 next_cursor 非空，点击下一页保留筛选并传 cursor；复制按钮得到完整原文件名与完整字符串 VID/MID |

- [ ] **步骤 4：执行模块验收与浏览器恢复回归。** 后端 `uv run pytest tests/modules/materials -q`；前端 `bunx playwright test tests/materials.spec.ts`、`bun run build`。预期目录分页完整、提示准确、stored 后页面关闭不取消任务、单项失败可重试、不同来源账户记录正确、跨租户无文件访问。真实 SDK 上传/分享/回查另列联调结果，不能由 mock 通过替代。
- [ ] **步骤 5：提交本任务。** 在应用根目录执行 `git add frontend/src/features/materials 'frontend/src/routes/_layout/tenants.$tenantId.materials.tsx' frontend/tests/materials.spec.ts frontend/src/client backend/app/modules/materials/router.py`，然后 `git commit -m "materials: add batch upload and asset workspace"`。

## 完成条件与衔接

五个任务完成后，策略预览只使用 `match_materials/get_material_readiness`，完整读取分页候选并在搭建快照中保存分组。执行计划在提交后使用 `ensure_target_asset` 等待真实目标资产，直接启用广告的业务规则由执行模块负责，素材模块不增加广告审批或启用流程。
