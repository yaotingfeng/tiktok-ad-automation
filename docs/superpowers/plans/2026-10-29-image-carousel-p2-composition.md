# P2 批量编排与配乐实施计划

> 实施衔接（2026-10-29）：P1 已按官方 SDK 单通道实施。纯编排输出“已有单元 ID / 待分配的新单元”，新实体 UUID 由 T2 持久化试算计划时一次分配；不得在纯算法中随机生成或从内容摘要推导实体 ID。候选快照补充 `upload_batch_created_at` 以实现设计规定的批次排序。T1 先增加类型和单元模型，现有视频草稿的数据回填及当前读写切换移至 P3-T1，与全部 unit_id 规划调用方同步完成，避免中间双写或陈旧镜像；图片路径直接使用新模型。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 遵循项目按需技能偏好。

**Goal:** 一次规则配置完成多部剧的图片组合、配乐、差异应用和可复用轮播。

**Architecture:** 无 IO 的纯编排器输出草稿创意；有状态服务负责候选快照、试算、generation 切换和乐观锁。推荐后台分页运行，创意内容与音乐版本各自有栅栏。

**Tech Stack:** FastAPI/SQLModel、Alembic、PostgreSQL、Celery/Redis、React/shadcn、pytest/Playwright。

**Spec:** [批量设计](../specs/2026-10-28-image-carousel-ads-design.md) §2–5、§7；前置 [P1](2026-10-29-image-carousel-p1-assets.md)。

## Global Constraints

- N=1 为单图；轮播 N=2–35，默认3；尾部默认保留；每剧独立，不能跨剧拼图。
- 规则优先级：手动保护 > 逐剧字段 patch > 整批；广告结构覆盖只支持整批。
- `PRESERVE_MANUAL` 默认；`REPLACE_ALL` 必须通过显式差异页选择；不悄悄清除候选排除。
- 未 READY 可保存草稿但不能提交；不新建广告对象；平台推荐不按目标账户倍增。
- 同图跨创意复用需显式动作；同创意内部不重复；缺图不能导致自动重切。

## Review Focus

- 02/2/10 同名及数据库分页边界 → T1 稳定排序。
- 61 图/N5 尾部单图及全排除空集 → T1 计数与阻断原因。
- 只锁音乐的旧组合被新 N 拆掉 → T2 冲突不能猜迁移。
- 试算后推荐完成 / 用户换曲 / 撤销绑定 → T2/T3 分别处理数据变化。
- 10,000 图片前端全量载入及后台超时 → T3/T5 分片、分页、断点与聚合查询。

### P2-T1：纯规则与草稿创意模型

**Files:** 新建 `backend/app/modules/builds/image_composition_schemas.py`、`image_composition_models.py`、`image_composition.py`；修改 `models.py`、`schemas.py`、`drafts.py`；新迁移 `*_image_composition.py`；新增测试 `backend/tests/modules/builds/test_image_composition.py`、`test_image_composition_migration.py`。

**Interfaces:** 在 schemas 定义：

- `ImageRule(mode: SINGLE|SEQUENTIAL, images_per_unit: int, order: NATURAL_NAME|UPLOAD_SEQUENCE, remainder: KEEP|EXCLUDE, music_mode: RECOMMEND|FIXED, fixed_music_id: str|None, single_music_id: str|None)`；rule patch 只含显式覆盖字段。
- `ImageCandidate(material_id, file_name_snapshot, upload_batch_id, upload_batch_created_at, upload_sequence, created_at, content_digest, enabled)`；允许缺旧上传序号，排序规则见 spec。
- `CreativeUnit(id, kind, ordered_material_ids, content_digest, music_id, music_rule_digest, composition_protected, music_protected, position, revision)`。
- `CompositionDiff(units, retained_ids, removed_ids, excluded_images, conflicts, rule_digest, candidate_digest)`。
- `resolve_image_rule(batch: ImageRule, patch: dict) -> ImageRule`；`compose_images(candidates: tuple[ImageCandidate,...], rule: ImageRule, previous: tuple[CreativeUnit,...], mode: PRESERVE_MANUAL|REPLACE_ALL) -> CompositionDiff`。函数禁止数据库、推荐或随机 IO。

- [ ] 测试断言 60/N5=12、61/KEEP=13、61/EXCLUDE=12且排除1；单图N1、非法0/36、同图不在同单元重复；2在10之前；分页来源合并后与一次输入同序；受保护图片不再次自动使用；同输入同规则相同分组。运行 `uv run pytest tests/modules/builds/test_image_composition.py -q` 观察失败。
- [ ] 实现纯算法、字段 patch 和确定性摘要；复用已有 creative_id，新单元使用明确的待分配提案类型，T2 持久化计划时生成 UUID，与 content digest 分离。T1 不建立视频草稿镜像；视频回填/当前读写切换由 P3-T1 同时完成。
- [ ] 迁移测试覆盖 VIDEO 默认值、租户/BC 外键、同图多单元；旧视频重复内容规则不放开，运行新测试及现有 `test_drafts.py`。视频顺序保留的实际回填测试归 P3-T1。
- [ ] 检查不存在把 unit_id 塞进 material_id 外键的捷径，聚焦提交。

### P2-T2：候选快照、试算、原子应用

**Files:** 新建 `backend/app/modules/builds/image_composition_service.py`、`image_composition_api.py`、`image_composition_tasks.py`；修改 `api.py`、`draft_tasks.py`、`draft_catalog.py` 及现有 outbox 注册位置；新增 `backend/tests/modules/builds/test_image_composition_service.py`。

**Interfaces:** 新 `ImageDraftScope(tenant_id, bc_id, draft_id, actor_id, route: FrozenTikTokRoute)` 由服务端授权解析，不直接信任请求体；`preview_composition(session, scope, request_id, expected_revision, rule, drama_patches, mode) -> CompositionPlan`；`apply_composition(session, scope, plan_id, request_id, expected_revision) -> CompositionRun`；`advance_composition(session, run_id, generation, limit=20) -> RunProgress`。CompositionPlan 持输入摘要、动作、差异和基准 revision；RunProgress 含状态、处理计数与游标。

手工修改同样提供服务端 API：`edit_creative_units(session, scope, request_id, expected_revision, command: CreativeEditCommand) -> DraftEditResult`。命令用严格判别联合覆盖 `SET_IMAGES`（增删/重排后的完整有序图片）、`CREATE`、`DUPLICATE`、`MERGE`、`SPLIT`、`DELETE`、`SET_PROTECTION`、`RESTORE_CANDIDATES`，每种只接收必要的 unit_id/图片引用/分割位置/保护字段。返回新的 draft revision、受影响单元与待配乐状态。新增图片必须先纳入同剧的授权候选快照；同图复用须显式命令。所有增删/排序/合并/拆分由后端验证 1–35 图及不重复约束，前端不直接写单元表。

- [ ] 测试：手动内容受保护；只锁曲且组合消失时冲突；改批次A曲到B只影响未保护项；恢复候选才能复活已删除图片；同request_id不同body拒绝、重复同body返回原任务；规则/候选变更使 plan 409。运行 `uv run pytest tests/modules/builds/test_image_composition_service.py -q` 观察失败。
- [ ] 实现候选快照与跨剧冲突解析；只读试算不写 active 创意。规则、候选、手工选择改变 draft.revision；系统配乐完成只改变 music_revision。应用读取最新可继承音乐必须符合内容/规则/请求代数，详见 spec §5。
- [ ] 实现手工修改 API 的同事务幂等/CAS、单元版本、两类保护与候选排除。图片编辑自动保护 composition；换曲仅保护 music；复制/合并/拆分创建新单元身份，复制继承原曲及其 music 保护状态。删除自动单元记录候选排除，解除保护不等于立即重生成。已有曲目的图片编辑按 spec 保留曲目并提示可重推；合并/拆分不猜选哪个原曲，进入待配乐，涉及受保护音乐先返回冲突。测试每类操作、跨剧/跨租户拒绝、过期 revision、重复请求及删除后不复活。
- [ ] 实现 generation 暂存/原子指针切换、单草稿单运行、应用前 CAS、分页续作、取消和失败保留旧 active；恢复只能接回相同计划，不能重算成新分组。外部 IO 不持事务锁。
- [ ] 用真实 PG/Redis 测试同时编辑/应用、Worker 中途退出/重放、切换前失败、推荐进度不导致无谓409、手工选曲必须409；断言一次切换且无半新半旧创意。定向通过后提交。

### P2-T3：音乐准备与进度

**Files:** 新建 `backend/app/modules/builds/carousel_music.py`、`carousel_music_tasks.py`、`carousel_music_models.py`、`carousel_music_api.py`；修改 `api.py` 及 jobs/outbox/dispatch 实际注册位置；新增 `backend/tests/modules/builds/test_carousel_music.py`、`test_carousel_music_api.py`。

**Interfaces:** `request_music(scope, unit_id, expected_content_revision, expected_music_revision, request_id, action: RECOMMEND|RETRY|SET_MANUAL, music_id=None) -> MusicJobRef`；`advance_music(job_id, generation) -> MusicProgress`。调用 P1 MusicOperations；表持 route、source advertiser、fingerprint、request generation、结果快照及尝试错误。READY 选曲是事实，不是“同图始终同曲”的承诺。

向 MusicPicker 提供草稿范围的 `search_music(scope, keyword, cursor) -> MusicPage` 和 `read_music(scope, music_ids) -> MusicPage` API，后者刷新展示信息及过期试听 URL。服务端核验租户/BC/当前连接绑定并选择已记录的种子账户，再调用 P1 适配器；浏览器不能直连平台或自行提交任意 advertiser/route。分页响应沿用 P1 曲目字段和 cursor；读取不写选曲状态、不伪造目标账户投放资格。

- [ ] 测试：2,000不同组合×100账户仅2,000推荐作业；单图无指定曲NEEDS_SELECTION不调推荐；FIXED不调推荐；相同内容/规则重生成不换曲；改music规则替换未保护音乐；手工曲覆盖晚到推荐；0时长不自动选；搜索超时≠曲目失效。运行 `uv run pytest tests/modules/builds/test_carousel_music.py -q` 观察失败。
- [ ] 实现搜索/read API 与展示信息归一化，测试分页、空结果、试听 URL 刷新、超时分类、绑定撤销和跨租户/BC拒绝；运行 `uv run pytest tests/modules/builds/test_carousel_music_api.py -q`，不得返回授权密钥或原始平台凭据。
- [ ] 实现种子账户核验及新鲜 image_url、同批 fingerprint 去重、每片20单元/10秒、平台有界重试和独立状态。不要额外按账号并发发推荐；实际调用走已有配额准入。
- [ ] 实现双版本栅栏与绑定撤销检查：旧结果可保存为审计，不能覆盖当前 selection。未变有效规则才继承旧曲；相同请求的重试不触发第二次自动换曲。
- [ ] PG/Redis 验证 generation 切换、延迟请求、断点续跑；聚合进度无需读取全量图片行，查询次数随分页而非图×账户增长。通过后提交。

### P2-T4：可复用轮播及引用保留

**Files:** 新建 `backend/app/modules/materials/carousel_presets.py`、`carousel_models.py`、`carousel_api.py`；修改 `router.py`、`object_uses.py`、`cleanup.py`；新迁移 `*_carousel_presets.py`；新增 `backend/tests/modules/materials/test_carousel_presets.py`。

**Interfaces:** `save_preset_version(scope, source_unit_id, expected_revision, name, request_id) -> PresetVersion`；`load_preset_into_draft(scope, preset_version_id, request_id, expected_draft_revision) -> CreativeUnit`。PresetVersion 保存tenant、来源BC、名称、顺序、音乐及内容摘要；载入生成独立单元并保护 composition/music，执行目标授权仍用草稿 route。

- [ ] 测试旧版本不可变、载入后改草稿不改源、库中升级不改已载入单元、成员图片不重复自动纳入、跨租户拒绝、引用删除保护；运行 `uv run pytest tests/modules/materials/test_carousel_presets.py -q` 观察失败。
- [ ] 实现显式另存、按名搜索、版本复制和引用保留；未完成配乐的预设只能作为待配乐草稿，不能绕过 READY 门槛。
- [ ] 新测试和 `test_object_uses.py`、`test_cleanup.py` 通过后提交。

### P2-T5：批量配置界面与契约

**Files:** 新建 `frontend/src/features/builds/ImageBatchConfig.tsx`、`ImageCompositionDiff.tsx`、`CarouselCreativeSheet.tsx`、`MusicPicker.tsx`；修改 `BuildInputPage.tsx`、`BuildPreparationPage.tsx`、`BuildDramaTable.tsx`、`DramaMaterialSheet.tsx`、`MaterialBatchPicker.tsx`、`api.ts`；素材库新增 `CarouselPresetList.tsx` 并接入 `MaterialsPage.tsx`；新增 `frontend/tests/image-composition.spec.ts`，修改 `frontend/playwright.config.ts`，更新 demo fixtures 与生成客户端。

**Interfaces:** 用 P2-T2 的试算/应用/手工修改响应与 P2-T3 聚合状态和搜索/read API，UI 不复制自然排序或广告计数算法。分页creative与image子项；服务端同质草稿校验优先于隐藏按钮。

界面入口、按钮和保存时机按[操作说明](../specs/2026-10-29-image-carousel-ui-flow.md)落实：先「查看生成结果」，再「应用并生成」；图片编辑暂存在卡片，点「保存图片调整」才写入；选曲点「使用这首音乐」才写入；侧栏关闭不能隐式保存未确认编辑。定向 E2E 补齐取消、未保存离开及待处理时预览禁用。

- [ ] 将 image-composition 和后续 image-delivery 两个文件名加入 Playwright 的 workspaceSpecs，沿用工作台传输边界模拟。浏览器用例断言一次配置多剧、字段覆盖/恢复、61图单图尾部待配乐、批量改曲、取消差异不改原结果、手动保护保留、冲突解决、失败重试、页面刷新恢复进度。运行 `bunx playwright test tests/image-composition.spec.ts --project=workspace` 观察业务断言的预期失败，不以真实登录 setup 失败代替。
- [ ] 构建“整批规则→应用并生成→按剧摘要→只处理问题”界面；所有新增布局使用现有 shadcn 和黑色主按钮；无音乐ID/请求协议术语暴露给普通用户。
- [ ] 服务端返回不可提交时禁用生成可提交预览；试算理论数标未核验，不能显示成最终广告数。复制另一媒体类型创建新草稿，不覆盖当前草稿。
- [ ] 根目录生成客户端，跑定向 E2E、`tests/materials.spec.ts`、`tests/build-preview.spec.ts` 和前端构建；更新阶段进度后提交。P2 不执行真实广告发布。
