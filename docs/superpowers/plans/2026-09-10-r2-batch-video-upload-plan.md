# R2 临时中转、批量视频上传与自动清理 Implementation Plan

> **For agentic workers:** 按 `superpowers:subagent-driven-development` 或 `superpowers:executing-plans` 分任务实施，沿用用户按需使用技能的偏好。用户已确定 R2 临时中转、批量上传、广告账户确认后删除原件；本文件是视频上传的独立实施计划，不能以找到历史设计或 S3 替身测试通过代替实现完成。

**Goal:** 大批本地视频可靠上传至 Cloudflare R2，经 TikTok 官方 SDK URL 入库与回读确认后自动删除 R2 原件，后续搭建继续使用 TikTok 账户素材。

**Architecture:** 扩展现有 materials 模块、PostgreSQL 状态与 Celery 三类队列，保留一个素材主记录与实际账户映射。新增有界导入会话、临时对象代次、原件使用记录及独立清理任务；上传、平台可用、临时文件清理分别计数。先交付不依赖 R2 原件的账户间分发，再开启自动删除。

**Tech Stack:** FastAPI / SQLModel / Alembic、PostgreSQL、Celery / Redis、固定版本 TikTok Python SDK、boto3 R2 S3 API、React / shadcn/ui / IndexedDB / Playwright。

**Spec:** [R2 临时视频上传设计](../specs/2026-09-10-r2-transient-video-upload-design.md)。历史材料为工作区 `outputs/unclassified/20260904-r2-tiktok-speed-test/system-design.md` 及同目录 `report.md`；项目原设计为 [素材中心](../specs/2026-09-08-tiktok-03-materials-design.md)。新设计覆盖旧文件的“永久保留原件”规则，不复制历史 BC、账户 ID、桶或凭据。

## Global Constraints

- TikTok 一律使用已固定的官方 Python SDK，不调用 MCP、CLI 或自造 TikTok HTTP 网关。
- 租户、BC、当前连接和真实操作人范围必须贯穿 API、对象键、任务和目标素材；不跨租户按哈希共享对象。
- 用户不配置素材账户。每个文件由系统选择一个合法来源广告账户，并保存这次实际上传位置；来源账户可按负载分散。
- 上传不识别或绑定剧目；文件名需包含完整剧名，搭建时再匹配。数据库中的文件名、摘要、来源和 VID/MID 在清理后仍保留。
- R2 仅临时中转。本次自动选择的来源账户完成强校验与回执落库后，立即安排删除，不等待未来广告批次或所有未来目标账户。
- 删除原件前，系统必须已具备经过验证的 TikTok 账户间共享或授权源 URL 转存路径。无法证明权限或结果时显式阻断，不能假装目标素材已存在。
- 已发送且结果未知的上传/共享先回查；不因为超时、Token 更新、对象 URL 过期就改路径再创建。
- 单文件、单次发送与任务时限均有界。现有文件 SDK 的 256 MiB 内存约束不能冒充 TikTok 官方 URL 上传限额。
- 主 API 不转发视频；摘要校验 Worker 会有界地流式读取 R2，必要的临时校验文件在 finally 中删除。不能声称所有服务均不读取视频字节。
- 日常 10,000 文件、突发 20,000 文件是工程验证目标；历史 60 文件样本不能证明当前部署的日吞吐。
- 仅测试自己创建的对象前缀；不扫描或删除用户已有 R2 数据。真正 Cloudflare / TikTok 验收需明确的实际部署与租户授权。

---

## 当前差距与交付顺序

基线 `a310428` 的 `storage.make_s3()` 支持泛用 S3，但 `uploads.initialize_object_upload()` 发送 `ACL="private"`，R2 不支持该 ACL 参数。`sdk_assets.upload_video()` 只用 `UPLOAD_BY_FILE`，文件摘要在 `open_original()` 下载期间计算。`readiness` 没有启用实际共享，删除原件会阻断新目标账户。没有删除任务、清理回执或存储字节预算。

现有 API 单批最多 200；批次列表有 SQL 分页，详情却全量返回，单文件查询也先读整批；上传页在浏览器中切片展示。不能仅把 200 改成 20,000。

实施顺序：Task 1 → 2、3 → 4、5 → 6 → 7 → 8。Task 4 前端与 Task 5 后端可在契约冻结后并行。自动清理启用必须等待 Task 6 和跨模块测试通过。账号与版权方自动续登属于另外两项交付，不把其测试计入本计划完成度。

## 文件与公共契约

沿用 `backend/app/modules/materials/`，不另建第二套素材系统。新增职责文件：

| 文件 | 职责 |
| --- | --- |
| `ingest_models.py` | 导入会话、临时对象代次、占用预算、对象消费者及清理台账 |
| `ingest_api.py` / `ingest_service.py` | 导入会话、子批次、分页及计数；只受理业务意图 |
| `object_budget.py` | 按全局及租户原子预留/释放暂存字节 |
| `object_validation.py` / `validation_tasks.py` | 有界流式摘要与媒体校验，持久化可信摘要，可靠投递及恢复 |
| `source_selection.py` | 从实际授权账户中按在途数量/退避状态选择来源 |
| `remote_sources.py` | 已授权 TikTok 素材的共享证据及即时源 URL，禁止永久保存签名 URL |
| `cleanup.py` / `cleanup_tasks.py` | 删除资格、发送前认领、删除/回查、释放预算与修复扫描 |
| `frontend/src/features/materials/upload-store.ts` | IndexedDB 保存可恢复元数据，禁止保存凭据、签名 URL 和文件字节 |
| `frontend/src/features/materials/upload-scheduler.ts` | 有界文件/分片窗口、背压、暂停与继续 |

新接口仍在 `/api/tenants/{tenant_id}/materials` 下；既有 ≤200 文件上传接口保留到客户端切换完成。以下定义是各任务共享的实现契约，执行时通过 OpenAPI 生成客户端，不手写第二套 TS API 类型。

```python
class IngestSessionCreate(BaseModel):
    bc_id: str
    request_id: UUID
    file_count: int = Field(ge=1, le=20_000)

class IngestFileInput(BaseModel):
    client_index: int = Field(ge=0, lt=20_000)
    file_name: str
    size: int = Field(gt=0)
    mime_type: str
    last_modified_ms: int | None = None

class IngestChunkCreate(BaseModel):
    request_id: UUID
    files: list[IngestFileInput] = Field(min_length=1, max_length=200)

class IngestFilePublic(BaseModel):
    material_id: UUID
    client_index: int
    file_name: str
    size: int
    generation: int
    upload_id: str | None
    part_size: int
    part_count: int
    can_retry: bool
    operation_revision: int
    platform_status: str
    temporary_storage_status: str
    received_bytes: int
    source_advertiser_id: str | None
    error_code: str | None

class IngestSummary(BaseModel):
    session_id: UUID
    accepted_count: int
    uploaded_count: int
    ready_count: int
    failed_count: int
    cleaned_count: int
    reserved_bytes: int
    stored_bytes: int
    next_cursor: str | None
```

新状态及约束：

- 平台状态沿用 receiving / stored / uploading / verifying / available / blocked / result_unknown 的语义，原件清理不把 available 回退。
- 临时对象状态为 waiting_capacity / reserved / receiving / stored / validating / verified / cleanup_pending / deleting / delete_unknown / deleted / missing。stored 只表示对象接收完毕，verified 才表示可信摘要落库；失败/过期是独立原因与重试字段，不等于对象已经不存在。
- 对象键为 `tenants/{tenant_id}/bc/{bc_id}/materials/{material_id}/{generation}/original`，新代次不覆盖旧键。对象摘要与身份在 HEAD/GET 时核对。
- `OriginalUse` 保存 object generation、业务 operation ID、消费者阶段和结束证据。租约仅用于认领，不能代替 TikTok 已停止拉取的证据。
- 累计一次的事实采用唯一 `(session_id, material_id, milestone)` 事件；当前 failed_count、阶段数量与 stored_bytes 使用唯一 `(object_id, generation, transition_revision)` 状态迁移事件，原子记录 before/after 并应用差量。失败后重试及新代次重传不重复累计素材数，但会转移当前失败/阶段/字节计数；清理释放按对象代次和证据唯一。汇总 DTO 文档逐字段标明当前值或累计事实。

## Task 1：持久化临时对象与导入会话契约

**Files:** Create `backend/app/modules/materials/ingest_models.py`、`backend/tests/modules/materials/test_ingest_models.py`；Modify `models.py`、`schemas.py`、`backend/app/alembic/env.py`；Create 新增 Alembic revision，revision ID 使用 `r2_transient_ingest`，parent 在执行开始时读取已合并的唯一 head，不改历史迁移。

**Interfaces:** 产生 `IngestSession`、`IngestSessionFile`、`TemporaryMaterialObject`、`OriginalUse`、`ObjectCleanup`、`ObjectBudget`、`IngestMilestone` SQLModel；现有 `MaterialFile` 和 `AccountMaterial` 保留原 ID、外键与审计身份。`MaterialFile` 的原件可用标记从新对象状态派生，平台素材可用性独立保留。

- [ ] 写迁移/数据库约束红测：相同租户 session 的 `client_index` 唯一；跨租户外键拒绝；同文件不同 generation 有不同 key；删除对象台账不删除平台映射。
- [ ] 使用真实 `_test` PostgreSQL 运行 `uv run pytest tests/modules/materials/test_ingest_models.py -q`，确认新增表/约束不存在而失败。
- [ ] 建表与必要索引：`(tenant_id, session_id, client_index, material_id)` 用于 seek 分页，`(status, next_attempt_at, id)` 用于恢复扫描；source 账户的在途计数独立于大批次头锁。
- [ ] 以历史 material/account/VID、原件 receiving/stored/unavailable fixture 升级；原件未发生外部删除前可回退本次新增结构。自动删除启用后仅回滚代码不能恢复字节，回滚手册须明确这一点。
- [ ] 验证增量计数采用唯一事件和原子 UPDATE，同一 `ready` 事件两次只增加一次；补充 failed→retry→available、两代次先后清理与重复迟到回执的数量/字节守恒测试；检查一百个独立素材的并发完成不会全表串行扫描。
- [ ] 提交 `feat(materials): add transient ingest and cleanup records`。

示例验收形态（fixtures 在本任务测试文件中创建真实租户、会话、文件，不连接外部）：

```python
def test_cleanup_does_not_remove_account_mapping(db, material, account_asset):
    old_vid = account_asset.video_id
    material.current_object.status = "deleted"
    db.flush()
    db.expire_all()
    assert db.get(AccountMaterial, account_asset.id).video_id == old_vid
    assert db.get(MaterialFile, material.id) is not None
```

## Task 2：R2 兼容传输、暂存预算和可信摘要

**Files:** Modify `backend/app/core/config.py`、`materials/storage.py`、`uploads.py`；Create `object_budget.py`、`object_validation.py`、`validation_tasks.py`、`tests/modules/materials/test_r2_storage.py`、`test_object_budget.py`、`test_validation_dispatch.py`；Modify `tasks.py`、`backend/app/jobs/celery_app.py`；Create `deploy/r2-cors.example.json`。

**Interfaces:**

```python
def reserve_object(session: Session, *, context: TenantContext,
                   object_id: UUID, byte_size: int) -> bool: ...
def release_object_reservation(session: Session, *, object_id: UUID,
                               deletion_evidence_id: UUID) -> None: ...
def validate_original(*, database_engine: Any, context: TenantContext,
                      object_id: UUID, s3: Any = None) -> None: ...
def sign_ingest_url(*, context: TenantContext, object_id: UUID,
                    operation_id: UUID) -> str: ...
```

- [ ] 先写 Botocore/传输边界契约：R2 CreateMultipartUpload 不传 ACL；region 为 `auto`；服务端生成指定 object generation 的 GET URL；凭据与完整签名 URL 不出现在日志、数据库、异常或 trace。
- [ ] 写 2 个并发事务抢最后暂存字节的红测：全局和租户额度同时判断、只一个成功；重复 reservation、清理回执重放不重复扣减。
- [ ] 增加显式 R2 provider 配置，使用 `S3_ENDPOINT_URL` / `S3_BUCKET` / `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY`，R2 endpoint 为实际账户 S3 域；不增加公开 `r2.dev` 依赖。配置样例不带实际账户或 key。
- [ ] 默认暂存窗口：全局 8 GiB、每租户 2 GiB，可由部署调整；文件并发 4、单文件分片并发 2、16 MiB part。它们是工程初值。预算满返回稳定 `storage_backpressure` 和等待时间，保留受理元数据，不签发新的 PUT/UploadPart。已预留文件可以继续完成。
- [ ] R2 CORS 仅允许应用部署 origin，PUT/GET/HEAD 与所需 Content-Type/校验 header，暴露 ETag；客户端遇到过期签名导致无法读取 CORS 错误时，重新向应用申请签名并回读已完成 parts，不能重新创建整文件。
- [ ] 独立校验任务 GET 流式读取原件，计算 SHA-256 与 MD5；必要时使用有界临时文件进行媒体检查，始终 finally 清除。不要将 multipart ETag 视为整文件 MD5。校验前后核对 generation、字节数和租户 metadata。
- [ ] 为原件读取、part PUT 和签名登记 `OriginalUse`，与清理资格争用同一行锁；网络 I/O 在事务外。原件预览使用期最长5分钟，源已具备清理资格后不续签，优先TikTok源预览；TikTok未知拉取不能按URL期限释放。
- [ ] 改造 `finish_upload`：完成接收事务写 exact validation outbox，不能直接发送旧 `materials.upload_original`；校验成功事务同时写可信摘要与 exact source outbox。任务 wrapper 注册到Celery，Beat有界修复失投任务，保留dispatch ID、generation、owner nonce和原退避时间；校验前后检查当前租户/BC、操作者权限及对象代次。禁止源任务紧密轮询“摘要未准备好”代替可靠阶段交接。
- [ ] 真实PG集成红测覆盖接收提交后broker丢消息、校验提交后source投递丢失、重复消费、过期worker回执和撤权；修复后只校验当前代次、只发一次源写意图。
- [ ] 运行 R2 契约、预算并发、原件损坏/中断/权限撤回测试及原 object_uploads 测试；提交 `feat(materials): support bounded private R2 staging`。

最低并发验收：

```python
def test_storage_budget_does_not_oversubscribe(two_connections, reserve):
    outcomes = run_concurrently(
        lambda: reserve(two_connections[0], 600),
        lambda: reserve(two_connections[1], 600),
        limit_bytes=1000,
    )
    assert sorted(outcomes) == [False, True]
```

本任务定义测试 helper `run_concurrently` 为两个 ThreadPoolExecutor future 加 barrier；每个调用使用自己的 Session，查询最终持久化预留合计，不能只测试 Python 内存计数。

## Task 3：大批导入 API 与真正的明细分页

**Files:** Create `materials/ingest_api.py`、`ingest_service.py`、`tests/modules/materials/test_ingest_api.py`；Modify `uploads.py`、`catalog.py`、`schemas.py`、`backend/app/api/main.py`。

**Interfaces:**

| Endpoint | 行为 |
| --- | --- |
| `POST /ingest-sessions` | 创建一次选择对应的会话，不传文件字节；返回 session_id |
| `POST /ingest-sessions/{session_id}/chunks` | 每次 ≤200 文件、稳定 request_id/client_index；立即返回受理结果 |
| `GET /ingest-sessions/{session_id}` | 只返回增量汇总和本租户 BC，不返回全部 files |
| `GET /ingest-sessions/{session_id}/files?cursor=&limit=100&status=` | 服务端 seek 分页，最大 100 |
| `POST /ingest-sessions/{session_id}/seal` | 冻结清单；不足/超额给逐项问题，之后禁止新增client_index；旧幂等chunk可回读 |
| `GET /ingest-sessions/{session_id}/files/{material_id}/parts?generation=&cursor=` | 服务端核对tenant/BC/material/generation/upload_id后调用ListParts，最多100项/页；不向浏览器交付R2凭据 |
| `POST /ingest-sessions/{session_id}/files/{material_id}/resume` | 回读当前multipart身份、part布局和操作版本；不另建同代次upload |
| `POST /ingest-sessions/{session_id}/files/{material_id}/part-urls` | 提交generation、operation_revision和有限part序号，核对预留/权限/未进入清理后续签；不返回整文件全部part签名 |

- [ ] 为 20,000 条元数据按 100 个子请求受理写红测：重复一个 chunk/request_id 返回同一批文件；同一个 client_index 换内容拒绝；相同文件名/大小的两个不同本地文件仍由 client_index 区分。
- [ ] 为完整接收前刷新、ListParts分页、已过期签名续签、旧代次/旧upload_id、租户切换及seal后追加写API回归。parts含part_number、byte_size、ETag；ETag只是R2分片接收证据，不是用户重选文件的内容身份证明。
- [ ] API 按每个新文件申请现有 MaterialFile/ObjectUpload 与 outbox；保留 ≤200 既有入口兼容。并发 chunk 用索引与唯一约束，不能对整会话长期持锁。
- [ ] 重写单文件 `get_upload_file_result()` 为按 tenant/BC/material ID 定点读取；`refresh_upload_batch()` 改增量 milestone，汇总修复仅在独立后台有界核对。每三秒轮询不得重读全部 20,000 文件。
- [ ] 验证分页跨界、过滤、无重无漏、不会泄漏另一个租户。用 SQL 查询计数/返回大小断言，500 文件与 20,000 文件每页查询数不随总行数增长；禁止仅测前端 slice。
- [ ] 生成 OpenAPI 客户端并确认旧 API 使用者行为兼容；运行 `test_ingest_api.py` 与材料 read APIs；提交 `feat(materials): accept large imports through bounded chunks`。

```python
def test_import_detail_stays_bounded(api, seeded_import):
    response = api.get(f"/ingest-sessions/{seeded_import}/files?limit=100")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 100
    summary = api.get(f"/ingest-sessions/{seeded_import}").json()
    assert summary["accepted_count"] == 20_000
    assert "files" not in summary
```

## Task 4：浏览器批量选择、分片窗口及刷新恢复

**Files:** Create `upload-store.ts`、`upload-scheduler.ts`；Modify `BatchUploadSheet.tsx`、`useUploadManager.ts`、`upload-transfer.ts`、`UploadQueue.tsx`、`BatchHistory.tsx`；Add `frontend/tests/r2-upload.spec.ts` 并注册 workspace project。

**Interfaces:** IndexedDB 按 `(tenantId, bcId, sessionId, clientIndex)` 存文件身份、part ETag、upload ID 和 operation revision；`scheduleUploads({maxFiles, maxParts, signal})` 消费被批准暂存的文件。账号/租户/BC 切换会停止旧范围签名与发送。

- [ ] 用 10,000/20,000 个小 File fixture 重现旧 200 限制和全列表渲染；单次拖拽或文件选择自动切 ≤200 子请求，UI 不要求用户手工分批。
- [ ] 列表用服务端分页；本地待传文件列表只渲染窗口。建立 Map 按 client_index 定位，禁止每行 `selected.find()` 与重复扫描全部文件。
- [ ] 上传窗口同时最多 4 个文件、每文件 2 个 part；上传完成一个 part 只保存对应记录，进度显示节流，不每个进度事件同步 JSON.stringify 全会话。
- [ ] 自动签名续期、失败 part 回读/续传；用户可以暂停本地未完成传输后继续。关闭弹窗不中断；切换租户/退出登录立即停止后续新请求。
- [ ] 刷新后通过服务端会话恢复已上传部分。浏览器丢失 File 引用时明确要求重新选择未完成文件，并核对名称、大小、lastModified 和已保存内容指纹；不能仅靠名称+大小重接另一份内容。IndexedDB丢失块摘要时，不得只凭ETag续传；有可信服务器块摘要则重新计算比对，否则先安全关闭旧代次，再以新代次重新上传。已完成到 R2 的文件不要求重选。
- [ ] 展示“等待上传 / 正在传输 / TikTok 处理中 / 可用于搭建 / 等待清理 / 已释放临时空间”；TikTok 可用和 R2 已删除分别展示。保留完整剧名命名提示。
- [ ] 测试超过 200、重名文件、1 个 part 断网、浏览器刷新、暂存满后自动继续、跨租户切换和签名 URL 不持久化。记录 20,000 选择时的响应时间、DOM 节点数和内存峰值，验收机器目标首反馈 1 秒内、可见行 ≤100，慢机器结果如实记录。
- [ ] 执行完整 TS、生产 build、materials 与 r2-upload Playwright；提交 `feat(materials): add resumable high-volume upload queue`。

```typescript
test("暂存窗口满时等候，释放后继续而不重复创建文件", async ({ page }) => {
  const api = await r2Boundary(page, { fileCount: 450, blockedAfter: 4 })
  await selectFilesAndStart(page, 450)
  await expect(page.getByText("等待可用上传空间")).toBeVisible()
  expect(api.maxConcurrentFiles).toBeLessThanOrEqual(4)
  api.releaseCompletedObjects()
  await expect(page.getByText("已受理 450 个文件")).toBeVisible()
  expect(new Set(api.acceptedClientIndexes).size).toBe(450)
})
```

本任务的 `r2Boundary` 只拦截应用 HTTP/签名对象传输，完整模拟续期与 part HEAD/list；最终 Task 8 另跑真实应用 API，不能只交付此边界测试。

## Task 5：官方 SDK URL 入库与自动来源账户分流

**Files:** Modify `sdk_assets.py`、`source_uploads.py`、`service.py`、`tasks.py`、`backend/app/modules/accounts/access.py` 中的 `assign_upload_account` 及其真实调用方；Create `source_selection.py`、`tests/modules/materials/test_url_ingest.py`、`test_source_selection.py`。

**Interfaces:**

```python
def upload_video_url(client: Any, *, advertiser_id: str,
                     video_url: str, remote_name: str, md5: str) -> object:
    return FileApi(client).ad_video_upload(
        access_token=client.default_headers["Access-Token"],
        advertiser_id=advertiser_id, upload_type="UPLOAD_BY_URL",
        video_url=video_url, file_name=remote_name, video_signature=md5,
        auto_bind_enabled=False, auto_fix_enabled=False,
        _request_timeout=(10, 90),
    )

def claim_source_account(session: Session, *, context: TenantContext,
                         bc_id: str, material_id: UUID) -> str: ...
```

- [ ] 固定 SDK 传输契约红测验证 `video_url` 被放入官方方法认可的 multipart 字段，未传 `video_file`，正确透传字符串账户 ID、原名和强摘要；签名 URL 被日志/异常过滤。若固定 SDK 字段行为与示例不一致，按真实本地 SDK 修正 wrapper 和测试，不换实现路线。
- [ ] 把 URL 入库与 FILE 入库的时限/字节限制分开。初始仍保留 URL 单文件 256 MiB 工程上限；超限在选择时逐项提示。专门用实际授权的 256 MiB 以上文件验证后才调整部署上限，不能把旧 SDK 内存限制说成平台硬上限。
- [ ] 替换“永远选择排序第一户”：在当前 tenant/BC 有效授权及上传能力账户中，按活跃在途数、最早冷却结束和稳定轮次选择；每文件认领结果落库。每来源初始 1 个并发，后续按真实配额配置，应用/租户/账户共享准入仍生效。
- [ ] 源任务由Task 2校验完成事务的outbox触发，回执与失投修复沿原dispatch推进。只有摘要已验证、来源已认领、共享准入成功时才签发 R2 GET；URL 不提前随批量入队产生。签名寿命默认 2 小时；业务状态保存 operation 和 object generation，不保存 URL。
- [ ] 在网络发送前持久化发送意图和 `OriginalUse`。上传返回 VID 后只进入 verifying；账号范围内 info 回读强匹配 MD5、VID、displayable 和业务要求的规格，成功后同事务写源映射与可用 milestone。
- [ ] 网络超时保留结果未知与原件；通过已知 VID / 确定名称+摘要核查，不切换来源重传。明确未发送或可靠失败才允许同对象新尝试/重新签名。
- [ ] 测试 20 个合法来源与 2,000 个准备任务不灌首户、单户冷却不饿死其他户；测试账号撤权、重复 worker、迟到回执、签名过期、缺/错摘要、收到 VID 但 info 空，以及同名不同内容。
- [ ] 运行源上传、SDK、锁序与恢复回归；提交 `feat(materials): ingest staged videos by URL across authorized accounts`。

## Task 6：原件清理后的账户分发和预览

**Files:** Create `remote_sources.py`、`tests/modules/materials/test_remote_only_distribution.py`；Modify `readiness.py`、`distribution.py`、`sdk_assets.py`、`catalog.py`、`schemas.py`、`frontend/src/features/materials/AssetDetails.tsx`；Review `covers.py`、`backend/app/modules/builds/preview.py` 与 execution 资源检查的真实调用位置。

**Interfaces:**

```python
def resolve_remote_source(session: Session, *, context: TenantContext,
                          bc_id: str, material_id: UUID,
                          target_advertiser_id: str) -> AccountMaterial | None: ...
def read_source_transfer_url(client: Any, *, advertiser_id: str,
                              video_id: str, expected_md5: str) -> str: ...
```

- [ ] 先写红测：R2 original 已 deleted，但源账户有效；新目标经 share 或 URL relay 完成验证，素材仍可预览/冻结/建广告；不能因 `storage_state != stored` 直接 blocked。
- [ ] 路径优先级：已核实目标映射 → 已知目标 VID 回查 → 有当前能力证据的原生共享 → 授权来源的即时 URL 转存 → 没有可信来源则明确要求补传。源账户不只依赖目录展示的一条 representative asset，要查询全部合法映射。
- [ ] 原生共享使用当前源与目标的明确权限及平台支持事实；接收响应后在目标账户查 actual VID/MID/摘要，不能把源 VID 直接当目标 VID。删除生产中无条件把 share 降级原件的逻辑，以可验证决策取代。
- [ ] URL relay 仅从当前有权访问且已经记录的源账户视频 info 取得新 URL；校验 HTTPS、无 userinfo、核对期望 VID/摘要及已核实的平台媒体域名，不接收用户粘贴 URL，不按 VID 前缀推断权属。通过同一个官方 SDK URL wrapper 入目标账户。
- [ ] 源 URL 只在本次发送内存中使用；队列重试重新查询。平台未知结果沿原 operation 回查，不从 share 切 relay 或反向切换；有限已知失败才产生新路径尝试。
- [ ] 清理后的素材预览从当前合法 TikTok 映射即时读取；GET 不触发外部上传/写入。拿不到预览 URL 显示“暂无法预览”，不把已存在广告素材标成原文件丢失。生成的封面继续走目标视频原有接口，不依赖 R2 原件。
- [ ] 测试源撤权但另一个映射仍合法、所有来源失效、源/目标同名不同内容、冻结前后发生清理、目标封面生成与重试，以及租户角色变化。
- [ ] 执行 distribution/readiness/covers/build 资源回归。真实租户至少验证一次源入库→删除专用测试 R2 原件→新目标分发→目标回读；该外部验收是开启清理的发布前置，不能用历史 2/2 或替身代替。
- [ ] 提交 `feat(materials): distribute account assets without retained originals`。

## Task 7：确认后立即删除、未知结果回查和孤儿回收

**Files:** Create `cleanup.py`、`cleanup_tasks.py`、`tests/modules/materials/test_cleanup.py`、`test_cleanup_concurrency.py`；Modify `source_uploads.py`、`uploads.py`、`catalog.py`、`tasks.py`、`backend/app/jobs/celery_app.py`。

**Interfaces:**

```python
def schedule_cleanup(session: Session, *, object_id: UUID,
                      source_receipt_id: UUID) -> UUID: ...
def run_cleanup(*, database_engine: Any, cleanup_id: UUID,
                 s3: Any = None) -> None: ...
def repair_cleanups(session: Session, *, limit: int = 100) -> int: ...
```

- [ ] 写不可误删红测：upload POST 返回成功但 info 尚未证实、未知发送结果、活跃 `OriginalUse`、对象代次不符时 DeleteObject 调用数为零。
- [ ] 源强回读成功的事务同时写 cleanup outbox。清理 Worker 认领时核对 source receipt、digest、当前 object generation；跟原件消费者/签名发放使用同一互斥资格，不在网络调用时持锁。
- [ ] 原件预览最长5分钟且源完成后不续签，清理最多等待已有预览使用期结束；已开始的远端拉取依据完成证据收尾，不用预览期限推断它结束。
- [ ] 对成功且没有未完成消费者的原件立即 DeleteObject，无固定保留天数。成功后 HEAD 确认不存在才写 deleted、释放预算和 cleaned milestone；响应丢失写 delete_unknown，后续 HEAD 404 即完成，仍存在才重试同一不可变 key。403 不是不存在。
- [ ] 清理任务是平台维护行为，受不可变 tenant/object/receipt 限制且写审计；原上传人停用不能使已成功对象永久占空间，也不能给清理任务任意 bucket/key 删除权限。
- [ ] 配置有界退避 10/30/60/120/300 秒；清理失败不回退 TikTok available、不触发重新上传。pending/delete_unknown 仍计暂存占用，额度满暂停新接收，恢复扫描按100项与任务预算推进。
- [ ] 用户取消/确认不可恢复失败的对象最长保留24小时工程默认；清理前确认没有远端未知读取。未完成分片在24小时无活动后关闭新签名/发送资格、收尾已有part使用记录，再显式AbortMultipartUpload。Abort200或HEAD404单独均不足以释放预留：已发part须结束或有可靠取消证据，针对exact upload_id回读ListParts/NoSuchUpload并确认HEAD不存在，迟到part出现则继续同代次清理核实，不能提前归还空间。R2生命周期保留7天未完成分片兜底，不对所有已完成对象设置盲目到期删除。
- [ ] 未知远端结果不能因24小时、签名到期、worker lease过期盲删；持续核查并限制新增占用。长期无法判定时列为异常待处理，不能宣称“任何文件都保证固定时间删除”。未知错误不需要普通用户判断登录过期，但无法判定上传结果确实需要运维证据。
- [ ] 孤儿扫描只遍历本应用管理前缀，与 generation 台账对账；未有所有权证据的对象只报告。清理已放弃已知对象前核对最近活动/消费者，绝不整桶清空。
- [ ] 测试删除成功回执丢失、删除前新消费者竞争、重复cleanup、旧generation迟到回执、角色撤回、R2暂时故障、Abort丢响应、并行PUT与abort、重复abort、迟到part和旧代次回包，以及清理后重试不会被错误路由到“重新上传原件”。
- [ ] 更新UI独立状态：平台可用、清理中/失败、临时空间已释放；数据库原文件名/摘要/VID/MID/来源/任务与审计不删除。提交 `feat(materials): release staged objects after verified ingestion`。

```python
def test_unknown_ingest_never_schedules_object_delete(app_state, cleanup):
    app_state.sent_upload_without_response()
    app_state.advance_beyond_worker_lease_and_url_expiry()
    cleanup.repair()
    assert cleanup.delete_calls == []
    assert app_state.reserved_bytes > 0

def test_lost_delete_reply_is_read_back_without_reupload(app_state, cleanup):
    app_state.verified_source_receipt()
    cleanup.delete_succeeds_but_response_is_lost()
    cleanup.repair_with_head_not_found()
    assert app_state.object_status == "deleted"
    assert app_state.platform_status == "available"
    assert app_state.video_upload_calls == 1
    assert app_state.reserved_bytes == 0
```

上述 `app_state/cleanup` fixtures 在本任务以真实 PG/outbox 和传输替身实现，两个 SQL Session 并发覆盖同一原件的清理/使用资格，不用一段 Python if 判断代替数据库竞争。

## Task 8：完整验收、部署与逐步放量

**Files:** Create `backend/tests/acceptance/test_r2_ingest.py`、`frontend/tests/acceptance-r2-ingest.spec.ts`、`scripts/acceptance-r2-capacity.py`、`scripts/check-r2.py`、`docs/acceptance/r2-ingest.md`、`docs/runbooks/r2-video-upload.md`；Modify 现有 CI、material acceptance transport 和部署说明中相应段落。

- [ ] 在专用 `_test` 数据库完成真实 FastAPI/JWT→导入→R2兼容传输边界→源入库→目标分发→清理；UI不伪造API成功。录下清理后数据库保留的素材身份、目标已验证VID和零原件读取。
- [ ] 10,000 常态、20,000 突发元数据运行，覆盖恢复后无重无漏、有限临时空间、分页和账户公平性；1,000 个任务混入限流/过期签名/超时/重启，记录最终状态和所有外部创建次数。禁止自动向真实平台发送这些容量任务。
- [ ] 记录峰值 R2 暂存、预留、清理积压、入库个数/分钟、P50/P95、页面响应、SQL请求量、Worker内存；区分合成调度时间和真实网络耗时。不把历史 36MB/s 或 4.29个/分钟写为新系统达标值。
- [ ] `check-r2.py` 默认只读配置校验；显式 `--probe` 时只在新建随机测试前缀进行小文件Multipart/HEAD/GET/Delete/HEAD404闭环。无账户key时报缺失字段名，绝不向运营桶试探默认值。部署脚本不自动创建桶或更改全桶权限。
- [ ] 用Linux prefork运行完整消费者，单Beat、共享Redis准入；Mac solo只供基本页面诊断。重启 API/Worker 不丢已受理文件、operation或清理任务。
- [ ] 真实环境先明确 R2账户、私有桶、CORS origin、部署密钥、租户/BC授权与本次素材来源范围，再测小批。顺序是R2兼容→官方SDK源入库→原件删除→新目标分发/封面回读→恢复演练，禁止先对全量素材打开删除再验证分发。
- [ ] 将“允许新上传”和“允许自动清理”设为独立部署开关；回滚先停止新摄入/新删除，已有删除回查可继续，保留所有远端映射。不尝试用数据库回滚伪造已经删除的原件恢复。
- [ ] 完成验证记录、确切提交和截图，提交 `test(materials): verify R2 ingestion cleanup and daily-volume queues`。

## 完成判断与交付证据

一份计划不算视频上传已完成。实现交付必须同时满足：

1. 用户一次选择超过200个文件，系统自动分批、窗口传输，能暂停/刷新恢复。
2. R2私有分片与GET签名真实兼容，不将key或完整签名URL写进日志/状态。
3. 合法来源账户自动选择，成功以平台回读为准，记录实际账户与VID/MID/强摘要。
4. 源入库确认后自动清理原件并回读确认，不删除素材主记录；清理失败有恢复和背压。
5. R2原件删除后，已有/新目标素材、搭建匹配、冻结、目标封面及失败恢复仍有完整路径。
6. 10,000/20,000工程验收有可重复记录；真实日吞吐及外部兼容另有授权环境证据。

当前本计划的实现步骤均未执行；R2实际账户/桶/凭据以及授权后的外部验证仍是部署输入。不得复用历史测试目录中的运营凭据替代用户的部署配置。
