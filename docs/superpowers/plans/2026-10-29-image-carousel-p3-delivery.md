# P3 结构规划、冻结与交付实施计划

> 实施修订（2026-10-29）：用户已授权在新 worktree 开始实施。核对实施进度中的 2026-10-15 明确决策后，本功能以官方 API/SDK 为交付通道；不扩展已退役中的 MCP，不重新开放其授权入口。下文旧双通道条目按此修订执行：P1-T1 记录历史 MCP 证据但只扩展当前 SDK 能力，P1-T4 不新建 mcp_music.py，新增音乐能力对退役连接明确不支持；集成门禁遵循当前 `not mcp` 范围。既有公共解析器仍保持严格数据合同。完整拆除 MCP 属于独立任务。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 遵循项目按需技能偏好。

**Goal:** 将批量生成的图片创意接入现有广告结构、冻结、执行恢复和广告报表，完成可验证的发布链路。

**Architecture:** 结构规划器处理不可拆分的创意单元；冻结 V2 保存有序图片和曲目。平台创建按格式编译并按同一意图回读，目标账户缺图不改正文。

**Tech Stack:** FastAPI/SQLModel、PostgreSQL/Redis、官方 SDK/MCP、React、pytest/Playwright。

**Spec:** [批量设计](../specs/2026-10-28-image-carousel-ads-design.md) §3、§6–8；前置 [P2](2026-10-29-image-carousel-p2-composition.md)。

## Global Constraints

- `N` 图片/单元、`M` 单元/基础广告、`K=creative_count` 文案版本是三层概念；现有两层分配行为保持。
- 轮播1–35图不可拆；单广告最多50单元并服从更严格场景限制；全批最终广告上限10,000。
- 所有账户复用图片顺序和music_id；目标image_id分别分发回读；新尝试重新核验音乐，UNKNOWN不得重建。
- 历史冻结正文/哈希不可变；不保留猜测型旧业务回退；旧任务排空与V1未提交预览处理按spec执行。
- 无授权不能创建远端广告；正式产品提交具体预览后ENABLE。

## Review Focus

- FIXED/SHARED与K倍数叠加预算 → T1。
- 视频场景资格缓存错误用于轮播 → T1格式scope。
- 同图不同音乐被去重或顺序丢失 → T2内容摘要。
- 某账户缺一图导致整轮播缩水 → T3原子性。
- 历史V1未提交预览及UNKNOWN任务漏在切换点 → T5迁移与提交测试。

### P3-T1：统一创意单元结构规划与试算

**来自 P2 的切换责任：** 本任务同时迁移未冻结视频草稿为一素材一单元，并将当前视频草稿读写与所有规划调用方切换到单元模型。P2-T1 只先建类型/表，不双写视频镜像；本任务分配实际新 Alembic revision，按原分组顺序回填，保留草稿 request_id，不重排视频文件名。以既有视频草稿新增/编辑及迁移夹具验证切换前后结构等价；历史冻结正文仍由原记录只读展示，不重写。

**前置审阅交接：** P2-T1 的 `CreativeUnitContent` 当前仅供图片算法使用；切换 VIDEO 单元生产者前，补齐与数据库一致的 VIDEO 音乐约束（`NOT_REQUIRED` 且 `music_id` 为空），以 schema 行为测试关闭该已登记次要项。

**界面审阅交接：** P2-T5 的本次结构控件文案在本任务同步明确：`creative_count` 显示“每基础广告文案版本数 K”，`max_materials_per_ad` 显示“每广告最多创意单元 M”，FIXED 模式不显示无效 M。

**Files:** 修改 `backend/app/modules/strategies/structure.py`、`builds/previews.py`、`preview_validation.py`、`scene.py`、`scene_constraints.py`、`scene_schemas.py`、`strategies/schemas.py`；更新 `frontend/src/features/strategies/StrategyStructureExample.tsx` 和 `frontend/src/features/builds/PreviewWorkspace.tsx`；扩展 `backend/tests/modules/strategies/test_structure.py`、`backend/tests/modules/builds/test_structure_preview.py`、`scene/test_scene_route_versions.py`。

**Interfaces:** 将结构输入重命名为 `NamedCreativeUnit.unit_id`，输出 `AdCreativePlan.unit_ids` 和 `GroupCreativePlan.unit_ids`；`plan_structure(units, *, config, pool, seed)` 保留原签名风格与分配顺序。接入 P2-T5 提供的 `effective_structure_config(strategy_version, draft_override) -> StrategyConfig` 只允许结构字段，不把预算/身份混入该patch。场景scope纳入 media_kind 和合同版本。

- [ ] 测试 60/N5/M6/K1/单SHARED组/10账户=20广告；K3=60；双SHARED组/K1=40；SEQUENTIAL_AVERAGE不足报错；轮播成员不被切片；视频旧样本逐组/逐广告输出与基线一致；同图不同曲两个unit不合并。先运行 `uv run pytest tests/modules/strategies/test_structure.py tests/modules/builds/test_structure_preview.py -q` 观察新用例失败。
- [ ] 改规划器单位及所有调用点，单元 ID不冒充文件ID。把共享/均分、文案pool去重、seed、已有单策略10,000及固定100限制保留；加全批10,000最终广告预估门槛，超限在跨账户展开前报错。
- [ ] 修改场景格式事实与限制维度；不能用已有SINGLE_VIDEO creative_fields生成轮播资格；场景测试证明不同media_kind/cache revision互不复用。前端示例使用服务器同语义结果，标明K为文案版本。
- [ ] 新测试、`scene/test_scene_route_versions.py`、前端 `tests/strategies.spec.ts` 通过后提交。

### P3-T2：冻结V2、编译与回读合同

**Files:** 修改 `backend/app/modules/builds/preview_models.py`、`previews.py`、`preview_materials.py`、`request_compiler.py`（含现有 ad_assets 函数）、`readback_compare.py`、`scene.py`、`contracts/builds.py`（完整路径为 `backend/app/integrations/tiktok/contracts/builds.py`）；新增 `creative_units.py`、迁移 `*_frozen_creative_units.py`；新增 `backend/tests/modules/builds/test_carousel_freeze.py`、`test_carousel_compiler.py`。

**规模与读取交接：** 同一草稿锁内以集合式 SQL 保存创意/成员快照及有序摘要，不逐单元查询或 flush；以真实 PostgreSQL 万张图片样例验证查询规模与一致性。所有现行读取、提交展开 SQL 和执行范围触发器通过显式合同版本读取冻结事实，不双写旧表。V2 大分组详情采用准确总数、明确命名的样例以及有界的组内广告分页，单条返回广告包含完整创意单元与图片；界面展示总数并支持翻页，不沿用100素材/100广告等任意组级展示限制，不将截断列表当作完整意图。相关 API/schema/生成客户端及 PreviewWorkspace 在本任务联动，V1 历史只读路径保持明确。

**Interfaces:** `VideoCreative(kind=VIDEO, video_id, cover_image_id)` 与 `CarouselCreative(kind=CAROUSEL, image_ids: tuple[str,...], music_id)` 严格判别联合。`frozen_ad_creative_units(session, tenant_id, preview_id, drama_id, group_no, base_ad_no)` 返回有序冻结单元；冻结单元子项包含 material_id/content_digest/music/version，目标ID通过冻结路由映射。新V2正文与历史V1区分，不从缺字段猜类型。

- [ ] 测试0/36图、缺曲、轮播带video_info拒绝；1/3/35图合法合同；换音乐/顺序改变摘要；同图不同曲保留两项；冻结后原图改名/预设升级不改变正文。运行 `uv run pytest tests/modules/builds/test_carousel_freeze.py tests/modules/builds/test_carousel_compiler.py -q` 观察失败。
- [ ] 实现新冻结表/触发器、摘要、严格编译、回读逐项比对及空映射阻断；图片主图与视频封面字段分清。所有待处理候选/配乐/未应用规则在冻结入口强校验。
- [ ] 历史冻结哈希/正文保持字节等价；新视频单元的wire请求与旧视频语义相同。新用例以及 `test_structure_preview.py`、`test_previews.py`、现 request compiler/readback 测试定向通过。
- [ ] 聚焦提交并同步展示字段，不在本任务迁移真实环境。

### P3-T3：逐账户准备、发送与恢复

**来源重试交接：** 同时修复 P1-T3 登记的来源占用账本次要项：`image_distribution.request_image_retry` 对确定未发送失败进行重试时，须通过共享 helper 恰好一次恢复此前 `_close_unsent` 已释放的 `SourceAccountLoad.in_flight/source_slot_held`；成功/终止释放恰好一次，UNKNOWN 仍只读核查。加入失败→重试→成功和重复重试的本地行为测试，不改变准入策略。

**Files:** 修改 `backend/app/modules/builds/execution.py`、`material_execution.py`、`recovery.py`、`cover_execution.py`、`submissions.py`、`preview_materials.py`，新建 `carousel_readiness.py`；新增 `backend/tests/modules/builds/test_carousel_execution.py`、`test_carousel_recovery.py`；扩展 `frontend/src/features/builds/SubmissionTables.tsx`、`presentation.tsx`。

**V2 交接：** P3-T2 先完成冻结内容与只读展示，P3-T3 将已知目标失败从整部剧×账户的 `BuildUnit` 细化到每条原定广告。冻结每广告排除原因并纳入摘要，提交展开、广告计数和预算必须使用同一事实；独立广告继续，不能因一张目标图失败排除同账户的全部广告。必要时新增小范围迁移，不改写历史冻结正文。同步接通图片草稿“生成搭建预览”的实际就绪条件，移除阶段性恒禁用占位；目标资格未核验仍明确阻断相应账户，不能伪造可投资格。

**Interfaces:** `verify_carousel_ad(route: FrozenTikTokRoute, advertiser_id: str, units: tuple[CarouselCreative,...], attempt_id: UUID) -> CarouselReadiness`。结果包含逐单元图片/曲目问题，禁止返回一个删减版units；成功才编译原冻结集合。P1负责资产调用，P2音乐只供已选ID，不调用recommend。

- [ ] 测试轮播2/3张就绪不发送、一个广告多个轮播其中一个失败则不发该广告、其他账户可继续；共享图只分发一次；音乐超时可恢复但不换曲；查询返回空与网络失败区分；UNKNOWN重放不产生第二个对象。运行 `uv run pytest tests/modules/builds/test_carousel_execution.py tests/modules/builds/test_carousel_recovery.py -q` 观察失败。
- [ ] 实现图片绕过封面任务、目标image_id精确映射、按attempt核验同music_id。预览已知失败明示排除广告；冻结后失败保留原计划并记录失败，不更新最终请求以缩减内容。
- [ ] 真实PG/Redis验证任务租约、晚到结果、授权代数改变及恢复。跑新测试、`test_execution.py`、`test_submissions.py`、`test_partial_material_execution.py` 确认视频原跳过规则不受影响。
- [ ] UI按“图片/音乐/身份/账户”显示可处理原因；当前任务素材ID不再标“目标VID”泛指所有类型。通过后提交。

### P3-T4：广告报表与目录最小支持

**Files:** 修改 `backend/app/integrations/tiktok/adapters/ads_read.py`、`adapters/sdk_report_reader.py`、`backend/app/modules/reporting/material_links.py`、`material_identity.py`、`schemas.py`、`backend/app/modules/ads` 中真实创意目录解码处；新增 `backend/tests/modules/reporting/test_carousel_ad_links.py`；修改自动规则素材类型筛选入口及对应前端能力提示。

**Interfaces:** 新 `CarouselCreativeIdentity(advertiser_id, ad_id, remote_creative_id?, ordered_image_ids, music_id)` 用真实可得字段构造，不能以图片集合代替有序组合。广告级度量保持原广告唯一键；创意/图片链接是说明性关系，不默认参与金额展开。

- [ ] 测试单广告两轮播共享一张图时广告消耗/收入只计一次；缺remote_creative_id时仍可广告级显示且不能伪造每单元收入；同图不同曲关联不混淆；图片不进入视频素材自动规则。运行 `uv run pytest tests/modules/reporting/test_carousel_ad_links.py -q` 观察失败。
- [ ] 实现目录/格式显示、冻结创意跳转、广告级指标；未确认粒度的图表明确只显示广告级。不添加按图均分或阈值猜测。
- [ ] 跑新测试和 `test_material_links.py`、`test_material_asset_identity.py`、`tests/modules/automation_rules/test_material_inputs.py`；确认系列/广告级规则维持原适用边界后提交。

### P3-T5：迁移、联调与发布验收

**Files:** 新建 `backend/tests/modules/builds/test_creative_unit_upgrade.py`、`frontend/tests/image-delivery.spec.ts`、`docs/validation/2026-10-29-image-carousel-batch.md`；更新 `docs/runbooks/deployment.md`、`docs/superpowers/specs/2026-09-16-tenant-material-library-design.md`、`2026-09-08-tiktok-05-build-execution-design.md` 与实施进度。

**Interfaces:** 本任务完成本地升级演练和明确验收记录，不新增运行接口；V1未提交预览一律只能历史查看，提交返回 `preview_contract_expired`，必须重新生成V2。已有提交幂等重放只返回原提交结果，不创建新任务。

- [ ] 测试可编辑视频草稿迁移、冻结正文/哈希不变、未提交V1拒绝新提交、已提交幂等请求返回原结果、仍有活动V1任务时发布检查失败；运行 `uv run pytest tests/modules/builds/test_creative_unit_upgrade.py -q` 观察失败后实现迁移/提交门槛。
- [ ] 从真实本地PG数据夹具升级并验证；模拟切换中止/恢复，禁止强行终结未知任务；验证保持现有功能开关/额度，不把新增业务参数当开关。数据库降级仅在未产生新格式数据且迁移实际支持时验证，不默认可回退。
- [ ] E2E覆盖批量上传→规则→配乐→异常修正→预览→模拟执行→恢复→报表；确认 P2 已将 image-delivery 纳入 workspaceSpecs，运行 `bunx playwright test tests/image-delivery.spec.ts tests/build-preview.spec.ts --project=workspace`，前端build及总计划检查通过。
- [ ] 真实平台联调单独准备可审阅操作清单：指定BC/连接/账户/图片/音乐/预算/停用结构；至少单图、三图、同一Ad两个轮播单元及目标账户共享回读。得到具体授权后执行，回读所有父对象DISABLE；未授权部分留待验，不伪称完成。35图边界和上传边界分别标明验证范围。
- [ ] 真实审核/音乐播放/滑动展示/价值优化与投入效果另需启用授权。上线前按实际生产 runbook 审查合同迁移方案并正常排空、升级；遵循 2026-10-29 最新规则，不默认创建备份或恢复演练，仅按明确选定方案执行。任何证据缺口清楚记录；本轮文档任务不执行这些线上操作。
- [ ] 核对端到端退出条件后聚焦提交并记录结果。推送/发布遵循用户另行授权与项目规则。
