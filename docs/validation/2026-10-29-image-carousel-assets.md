# 图片与音乐基础：能力证据与本地验证

核验日期：2026-10-29。阶段：P1-T1。此记录区分静态合同、合成 HTTP 回执测试和真实平台证据；本轮未调用真实广告、素材上传、授权或共享接口。

## 范围修订

2026-10-15 的明确用户决定是逐步退役官方 MCP、统一使用官方 API，见 [实施进度](../implementation-progress.md#2026-10-15官方-mcp-通道开始退役)。因此本轮保留 SDK 图片合同和既有共享解析，不新增 MCP 工具、音乐能力或授权入口。MCP 文件仅作为历史合同证据核查，不宣称 MCP-only 租户具有本期新增能力。不能改用另一条未授权连接来绕过缺失权限。

## 固定来源与事实

| 来源 | 核验结果 | 证据边界 |
| --- | --- | --- |
| [官方 SDK FileApi，固定 f809c396520df2d7b201a9ccc5378d822b728ed3](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/python_sdk/docs/FileApi.md)；本地 `backend/pyproject.toml` 与 `uv.lock` | `ad_image_upload` 使用 `/open_api/v1.3/file/image/ad/upload/`；`file_image_ad_info` 使用 `/open_api/v1.3/file/image/ad/info/`。生成入口为 multipart；本项目既有 URL 上传经同一官方 SDK `ApiClient.call_api` 发送 JSON | 固定 SDK 定义不等于当前平台实测；本轮不更换依赖或传输通道 |
| [固定 SDK FileImageAdUpload 原始模型](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/python_sdk/business_api_client/models/file_image_ad_upload.py) | URL、FILE、FILE_ID 上传输入；文件名 1–100 字符；文件上传有签名参数 | 模型没有明确图片编码格式、最大字节、宽高上限，均为 UNKNOWN；参数存在不证明任一账户有权使用 |
| [固定 SDK 图片编辑原始定义](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/yml_files/creative_image_edit.yml) | 存在 `is_carousel_usable` 输出映射 | 仅证明该字段在官方定义存在，不能证明图片 info/search 在所有账户或场景稳定返回 |
| `backend/app/integrations/tiktok/mcp/tool-contracts.json`，revision `2026-09-12.2` | 历史 tools/list 快照列出 `file_image_ad_upload`（URL/FILE_ID）、`file_image_ad_info_get`、`file_image_ad_search`；图片条目的 `output_schema` 为 null | 快照声明输入于 2026-09-12 核验，本轮未刷新远端；无输出字段及格式/字节/尺寸硬限证据 |
| [官方图片上传文档入口](https://ads.tiktok.com/marketing_api/docs?id=1739067433456642) | 本轮打开后重定向到 portal，浏览工具未取得正文 | 当前平台图片上传格式、字节和尺寸上限保持 UNKNOWN，不以搜索摘要或第三方文档替代 |

## 上传、推荐、广告使用分开判定

- `URLImageUpload` 继续只有目标账户、内部授权 URL、文件名，不携带音乐推荐规则。URL 来源授权及手动上传入口限制仍由上游负责；此合同不开放外部 push 或来源 URL 图片入口。
- 计划提到的推荐输入 50 MB/尺寸约束属于音乐推荐端点，不能迁移到图片上传。固定 SDK 与历史 MCP 图片合同未提供音乐推荐输入证据；本轮未取得其当前原始定义，因此具体推荐字节口径、宽高、格式与图数仍为 UNKNOWN，须在 P1-T4 独立核验。
- 主图可用性由目标账户 `ImageRecord.is_carousel_usable` 表达：严格布尔 true/false 原样保留；缺失或 null 为 None（未知）；字符串、数字、数组、对象返回 `unsupported_material_schema`。`displayable`、视频权限、上传成功回执或共享回执都不证明轮播资格。
- `read_images` 和 `search_images` 共用同一解析；搜索投影保留该字段。后续分发必须核验目标 image_id、目标账户及明确 true，缺失时保持未证实，不能直接就绪。
- SDK 已有图片权限校验保持：仅视频 scope 时上传返回 `cover_permission_unverified` / `NOT_SENT`，零 HTTP 发送；不能把视频能力推出图片可用。不新增可能让旧视频封面路径误判的新错误语义。
- 本次不改变 `ImageReceipt`：上传成功的 image_id 仍可确认入库，但它不代替轮播主图的目标回读。

## 本地运行资源限制

T1 核验时 `backend/app/core/config.py` / `.env.example` 的现有上传运行参数为：`MATERIAL_SDK_MAX_UPLOAD_BYTES=268435456`（256 MiB）、`MATERIAL_URL_MAX_UPLOAD_BYTES=1073741824`（1 GiB）。它们是既有视频路径的本地容量，不是图片平台上限，不直接复制为图片限制。存储预算参数默认 0（未配置），按原准入逻辑解释。

T1 基线尚无图片解码像素上限、图片专属上传字节参数或图片解码器。P1-T2 必须补独立参数、实际解码与额度回收测试，并在此记录最终名称/数值；没有这些实现不能声称手动图片入库已经可用。无新增部署级布尔开关。

## 验证

新增 `tests/integrations/tiktok/test_carousel_asset_capabilities.py`；使用真实官方 SDK 序列化、解析与适配器，只替换 urllib3 最终 HTTP 边界，全部账户/回执/URL 为合成值。

- RED：21 项中 20 failed、1 passed（0.27s）。失败原因是 `ImageRecord` 丢失资格字段与非布尔资格未拒绝；原有视频 scope 拒绝图片上传已通过。
- GREEN：同命令 21 passed（0.13s）。包含 `test_image_capability_is_not_inferred_from_video`、`test_upload_and_recommend_limits_are_independent`、read/search 三态与非布尔拒绝。
- 4000×4000、60 MiB 的测试输入只是合成平台成功事实，用于防止推荐门槛污染上传/读取合同；**不是已实测可上传尺寸或容量**。
- SDK 定向回归：74 passed、49 deselected（43.45s）；显式排除历史 MCP 参数。
- 共享图片/视频辅助层：`test_cover_sdk.py`、`test_sdk_assets.py` 共 27 passed（0.19s）。
- 修改 Python 文件的 Ruff 检查与格式检查均通过；未运行后端全量套件。

运行命令（仓库根目录）：

```bash
uv run --directory backend pytest tests/integrations/tiktok/test_carousel_asset_capabilities.py -q
uv run --directory backend pytest tests/integrations/tiktok/test_carousel_asset_capabilities.py tests/integrations/tiktok/test_material_contracts.py tests/integrations/tiktok/test_material_adapters.py tests/integrations/tiktok/test_cover_adapters.py -q -k 'not MCP'
```

## 真实联调待项

取得具体授权后，分别验证：实际图片格式/容量/尺寸边界；上传回执；源和目标账户 info/search 对 `is_carousel_usable` 的返回稳定性；IMAGE 原生共享后目标 image_id 与 true 回读；推荐接口当前原始限制；各 SDK scope 与目标账户适用性。T1 没有数据库迁移，也没有验证图片广告投放资格或创建广告。

## P1-T4：音乐只读适配器（2026-10-29）

新增 `gateway.music`，只使用当前冻结 `OFFICIAL_API` 路线的官方 SDK `ApiClient.call_api` 读取 `GET /open_api/v1.3/file/music/get/`。固定 SDK 没有音乐生成方法；已检查其原生 `async_req` / `_preload_content=False` 支持，以有界原始回执严格解析，未引入自定义 HTTP 客户端。退役 MCP 对三种音乐读取均返回 `music_channel_unsupported`，未新增工具合同、音乐 MCP 适配器或授权入口。

证据边界：端点列于[官方 Postman File music get](https://www.postman.com/tiktok/tiktok-api-for-business/request/n2bio7t/file-music-get)；[官方 Get the music list 文档](https://business-api.tiktok.com/portal/docs?id=1740053909509122)当前正文受开发者状态限制。详细请求字段和 `data.musics` / `data.page_info` 响应结构来自已发表工具元数据与历史已接受请求证据。**本轮未取得当前官方正文，未进行真实 SDK 音乐调用，不能将本地合成测试写成平台验收。** 未复制历史真实账户、URL 或完整回执。

接口与本地界限：

- `search(advertiser_id, keyword, cursor, budget)`：固定 `CAROUSEL_ADS` + `SEARCH_BY_KEYWORD`，每次仅一页；签名游标绑定完整冻结路线、账户、查询、场景和页大小，同一范围可跨 gateway 会话续读。密钥复用部署 `SECRET_KEY`，不新增密钥或持久状态。
- `recommend(advertiser_id, image_urls, budget)`：固定 `SEARCH_BY_RECOMMEND`；按本阶段合同本地校验 2–35 张 HTTPS 预览地址，不能传 image_id。返回首批最多 100 个候选；平台有后续页时 `MusicPage.truncated=True`、`next_cursor=None`，不自动翻页。2–35 是本阶段执行约束；图片字节、尺寸等当前官方限制仍待验，不作为上传硬门槛。
- `read(advertiser_id, music_ids, budget)`：固定 `SEARCH_BY_MUSIC_ID`；本地限 1–100 个不同 ID，拒绝未请求或重复 ID；缺失 ID 保持缺失。结果需要后续页时明确 `music_read_incomplete`，不以部分结果冒充完整读取。
- `MusicItem` 只保留 `music_id/display_name/author/duration/preview_url`，名称按 name → file_name → ID；零时长合法，缺失时长为 None。查询成功不生成投放资格或自动选曲。预览 URL 及游标均不出现在 DTO repr；证据只保留安全 request_id/数字业务码，不记录 provider 原文、凭据或请求 URL。
- 统一 `music.get` 授权/admission 操作和物理端点配额，三种读取不能拆桶；每个发送重新核验账户权限与冻结代次。沿用 `RemoteCallBudget` 与真实任务硬期限/租约约束；严格区分成功空列表、超时、限流、非法响应。

P2 调用方仍须使用 `gateway.materials.read_image_preview` 从实际 seed account 新鲜读取图片 URL，并在业务结果落库前复核冻结路线、当前权限与持久输入身份。T4 不创建选择策略、音乐收藏、任务或数据库表，不持久化预览 URL；原始 VIDEO/IMAGE 素材没有音乐字段。

本地验证：`test_music_adapters.py` **46 passed**；计划指定音乐/素材 DTO/素材适配器组合 **78 passed, 37 deselected**（当时含音乐 43 项，最终增加的 3 项由 46 项独立全套覆盖）；相邻 SDK 网关配额/范围回归 **8 passed, 34 deselected**。PG 与 Redis 使用专用本地测试实例；Redis DB14 按测试配额前缀清理，无 FLUSH。provider 只在官方 SDK 最终 HTTP 边界使用合成回执。Ruff、格式、ty、compileall 与 diff 空白检查通过。无广告创建、远端调用、推送或部署。

```bash
TEST_REDIS_URL=redis://127.0.0.1:6379/14 uv run --directory backend pytest tests/integrations/tiktok/test_music_adapters.py -q
TEST_REDIS_URL=redis://127.0.0.1:6379/14 uv run --directory backend pytest tests/integrations/tiktok/test_music_adapters.py tests/integrations/tiktok/test_material_contracts.py tests/integrations/tiktok/test_material_adapters.py -m 'not mcp' -k 'not MCP' -q
TEST_REDIS_URL=redis://127.0.0.1:6379/14 uv run --directory backend pytest tests/integrations/tiktok/test_sdk_report_reader.py -m 'not mcp' -k 'gateway or service_class or physical_report_aliases or content_identity_reads' -q
```
