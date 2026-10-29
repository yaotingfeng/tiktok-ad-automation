# P1 图片与音乐基础实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 遵循项目按需技能偏好。

**Goal:** 批量手动上传图片、生成可核验的目标账户主图映射，并提供音乐读取合同。

**Architecture:** 原始素材增加类型并复用现有 ingest、对象预算、分发状态机；图片不走视频封面路径。音乐使用受范围约束的独立读适配器。

**Tech Stack:** Python/SQLModel、Alembic/PostgreSQL、Celery/Redis、官方 SDK/MCP、现有 React 上传队列。

**Spec:** [批量设计](../specs/2026-10-28-image-carousel-ads-design.md) §4、§6、§7；另读[总计划](2026-10-29-image-carousel-batch-plan.md)。

## Global Constraints

- VIDEO/IMAGE 原始素材无音乐字段；图片 1–35 张的限制属于创意单元，不能限制一次上传文件数。
- 只开放手动上传图片；现外部 push/来源 URL 业务入口继续拒绝图片；内部已授权存储 URL 的平台上传仍可使用。
- tenant/BC/connection/binding generation 严格隔离；图片共享后必须在目标账户回读 image_id 与 is_carousel_usable。
- 推荐接口的 50 MB/尺寸不作为上传平台硬限制；无新业务开关；不创建广告。

## Review Focus

- MIME 伪装、损坏文件和解码炸弹 → T2 拒绝且正确回收额度。
- 同内容视频/图片去重键混用 → T2 类型隔离。
- 跨账户 shared receipt 被当成已就绪 → T3 必须回读。
- 只有 SDK 或只有 MCP 的租户能力缺口 → T1/T4 明确能力错误。
- 已绑定图片被临时清理 → T2/T3 延用 object_uses 引用与清理约束。

### P1-T1：能力证据与迁移准备

**Files:** 新建 `docs/validation/2026-10-29-image-carousel-assets.md`；读取/按真实差异修改 `backend/app/integrations/tiktok/contracts/materials.py`、`mcp/tool-contracts.json`、`adapters/sdk_materials.py`、`adapters/mcp_materials.py`；新建 `backend/tests/integrations/tiktok/test_carousel_asset_capabilities.py`。

**Interfaces:** 沿用 `MaterialOperations.upload_image_url/read_images/share_assets` 与已有 `URLImageUpload/ImageRecord`，先核对 is_carousel_usable 是否可稳定返回；新增能力错误只描述事实，不切换未授权连接。

- [ ] 核对所固定 SDK、MCP 原始合同：上传格式/字节/尺寸、推荐输入限制分别记录来源与时间。只读定义不要求广告写入；平台真实探测按具体授权进行，未知项保持 UNKNOWN。
- [ ] 新增测试 `test_image_capability_is_not_inferred_from_video`、`test_upload_and_recommend_limits_are_independent`：视频能力不得推出图片可用，推荐输入不合格不等于上传非法。运行 `uv run pytest tests/integrations/tiktok/test_carousel_asset_capabilities.py -q` 观察失败。
- [ ] 用现有合同补齐字段映射与可识别的不支持错误；确认本地字节/像素资源限制为独立运行参数，具体数值写入验证记录，不编造平台限制。
- [ ] 定向测试通过；列出真实联调尚待项。按总计划提交合同/证据，不包含真实请求、账户、素材 URL 或凭据。

### P1-T2：图片批量入库和读取

**Files:** 修改 `backend/app/modules/materials/models.py`、`ingest_models.py`、`ingest_schemas.py`、`ingest_service.py`、`object_validation.py`、`content_identity.py`、`repository.py`、`schemas.py`、`object_uses.py`、`cleanup.py`；新增 `image_validation.py` 与 Alembic `*_image_materials.py`（实际 revision 开工分配，包含 IngestFile 类型和批内 sequence 持久化）。前端修改 `frontend/src/features/materials/UploadPanel.tsx`、`ingest-manager.ts`、`MaterialTable.tsx`、`AssetDetails.tsx`；测试新增 `backend/tests/modules/materials/test_image_ingest.py`、`test_image_material_migration.py`，扩展 `frontend/tests/materials.spec.ts`。

**Interfaces:** `inspect_image(path: Path, *, max_pixels: int) -> ImageMetadata`，在新 image_validation.py 定义 dataclass，包含 format/width/height/byte_size；存量 VIDEO 的 duration 状态规则不变，IMAGE 不要求 duration。IngestFile 增加媒体类型和稳定批内 sequence；内容身份区分类型，保留已有视频键算法。

- [ ] 测试断言 JPG/PNG 真实解码、伪后缀拒绝、坏图拒绝、IMAGE duration=None、并发上传完成顺序不改变批内 sequence、去重不跨类型、已引用图片不被 cleanup 删除。迁移测试覆盖待探测视频无 duration。运行 `uv run pytest tests/modules/materials/test_image_ingest.py tests/modules/materials/test_image_material_migration.py -q` 观察失败。
- [ ] 实现模型迁移、验真和目录读取，沿用 ingest 配额、摘要和授权，动画/多帧图片本期明确拒绝为不支持，不悄悄取首帧；像素限制不改写图片。
- [ ] 更新批量上传及列表/详情，图片显示尺寸而非播放时长；push/来源 URL 入口类型校验不放开。生成客户端用根目录 `bash scripts/generate-client.sh`，审查生成差异。
- [ ] 跑上述测试、现有 `tests/modules/materials/test_ingest_api.py`、`test_content_identity.py`、`test_object_uses.py`；前端运行 `bunx playwright test tests/materials.spec.ts`。确认视频上传行为未变后聚焦提交。

### P1-T3：主图分发、补发与目标回读

**Files:** 修改 `backend/app/modules/materials/distribution.py`、`batch_distribution.py`、`batch_verification.py`、`cover_sharing.py`、`readiness.py`、`reissue.py`、`video_reissue.py`、`cleanup_abandoned.py`；新增 `image_distribution.py`；新增测试 `backend/tests/modules/materials/test_image_distribution.py`。

**Interfaces:** `prepare_image_targets(route: FrozenTikTokRoute, material_ids: tuple[UUID,...], advertiser_ids: tuple[str,...]) -> ImageDistributionResult`；FrozenTikTokRoute 复用 integrations/tiktok/contracts/context.py，权限从现有租户服务解析，ImageDistributionResult 在新 image_distribution.py 定义为 dataclass，只含本地状态/任务引用；provider 调用仍用 MaterialOperations。图片共享与封面共用底层方法，主图就绪判定独立。

- [ ] 新测试：shared receipt 但未回读不 READY；is_carousel_usable=false 不可用；某目标失败不污染别的映射；撤销绑定后的迟到结果被拒绝；重复投递不重复远端上传；图片不创建 CoverJob。运行 `uv run pytest tests/modules/materials/test_image_distribution.py -q` 观察失败。
- [ ] 实现按类型路由、目标回读与错误分类，复用现有任务预算/租约/outbox；同图多个轮播只分发一次到每个目标账户；补发不得以 image_id 当 video_id。
- [ ] 跑新测试以及 `test_distribution.py`、`test_batch_distribution.py`、`test_cross_bc_route_guard.py`、`test_object_budget.py`；需要锁并发的用例连真实 PG/Redis。
- [ ] 聚焦提交；真实“上传→至少两账户共享→回读”由单独授权执行并记录到本地证据，只在已验证后写验收结论。

### P1-T4：音乐读取适配器

**Files:** 新建 `backend/app/integrations/tiktok/contracts/music.py`、`adapters/sdk_music.py`、`adapters/mcp_music.py`；修改 `gateway.py`、`mcp/tool-contracts.json` 及实际 admission 注册点；新增 `backend/tests/integrations/tiktok/test_music_adapters.py`。

**Interfaces:** `MusicOperations.search(*, advertiser_id: str, keyword: str, cursor: str|None, budget: RemoteCallBudget) -> MusicPage`；`recommend(*, advertiser_id: str, image_urls: tuple[str,...], budget: RemoteCallBudget) -> MusicPage`；`read(*, advertiser_id: str, music_ids: tuple[str,...], budget: RemoteCallBudget) -> MusicPage`。适配器实例由 gateway 用 FrozenTikTokRoute 绑定；RemoteCallBudget 沿用 materials.py。MusicPage 含 items 和 next_cursor。MusicItem 固定 `music_id/display_name/author/duration/preview_url`，display_name 按 name→file_name→ID；URL 不是永久身份。实际 SDK 方法先经 T1 核实，不能用未经支持的 HTTP 填洞。

- [ ] 测试两通道归一化同样的分页/曲名空值，推荐一图本地拒绝、2–35图合法；请求上下文隔离；超时与空结果不同；查询成功不伪造投放资格。运行 `uv run pytest tests/integrations/tiktok/test_music_adapters.py -q` 观察失败。
- [ ] 实现映射、配额和超时，保留平台 request_id；不要在本任务实现选曲策略或平台收藏写入。
- [ ] 新测试通过，跑 `test_material_contracts.py`、`test_material_adapters.py`。验证 SDK/MCP 实际能力的证据分别标记；尚未接入的能力明确阻断对应连接验收。
- [ ] 更新验证文档和实施进度后聚焦提交。P1 完成不代表图片广告已可搭建。
