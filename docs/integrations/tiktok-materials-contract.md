# TikTok 素材分发契约

当前实现以本节为准。官方 Python SDK 固定修订仍为 `f809c396520df2d7b201a9ccc5378d822b728ed3`；MCP 使用本项目服务器的官方代码客户端及真实授权工具目录，不调用开发机的广告 MCP。

## 路径与授权

1. 当前目标账户有新鲜映射时复用。已有 VID 但映射过期时，只核实目标账户。
2. 同 BC 有合法源库存时调用原生 `/creative/asset/share/`，不走 URL 上传，也不依赖原件或视频 CDN 白名单。源/目标必须由发送授权同时具备操作权限。源资产所有权还由 TikTok 共享接口校验；工具存在不代表每一对账户一定成功。
3. 跨 BC 仅在同租户可访问且 SHA-256、MD5、文件大小一致的已验证库存间使用 URL 转存；各自冻结实际 BC、账户、连接与授权代数。目标素材库范围保持原样。
4. 没有可用平台来源的旧原件仍可按既有文件上传流程准备；新 R2 导入先完成源账户入库。已经发送或结果不明的操作绝不改路径重发。

共享调用使用源 MID；名称由 TikTok 复制。先取得源的实际 MID/名称，持久保存恢复线索，再发送共享。成功回执不虚构目标 VID；按目标账户名称和内容摘要查找，采用实际返回的 VID。首次查询立即调度，临时不可见或结果不明时继续有界查询。重名冲突也走目标匹配，不能改为重复上传。API 和 MCP 共享同一业务路径、限流、持久操作和恢复逻辑。

## 官方调用

| 操作 | API / MCP |
| --- | --- |
| 原生共享 | 官方 `CreativeManagementApi.creative_asset_share` / `creative_asset_share_get`，`advertiser_id` 为源，`material_ids` 为 MID，`shared_advertiser_ids` 为目标；DTO 限 20 个素材、10 个目标 |
| URL 上传 | 官方 `FileApi.ad_video_upload` / `file_video_ad_upload`；`UPLOAD_BY_URL`、实际目标账户、可识别文件名，关闭自动修复/绑定 |
| 视频详情 | `file/video/ad/info/` / `file_video_ad_info_get`，实际账户范围 |
| 视频搜索 | `file/video/ad/search/` / `file_video_ad_search`；共享恢复按源实际名称和摘要匹配，不假定目标 VID 等于源 VID |

官方生成方法见 [CreativeManagementApi](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/python_sdk/business_api_client/api/creative_management_api.py) 和 [FileApi](https://github.com/tiktok/tiktok-business-api-sdk/blob/f809c396520df2d7b201a9ccc5378d822b728ed3/python_sdk/business_api_client/api/file_api.py)。MCP 名称及输入 schema 已通过测试服务器真实 `tools/list` 读取核对；业务成功必须另看实际共享/查询记录。

## 文件名、封面和响应

本地 `MaterialFile.file_name` 保留完整原名。新上传/URL 分发使用可识别原名加短关联后缀，完整 UTF-8 名称最多 100 字节，超长从中间压缩保留末尾编号。原生共享按平台规则保留源名称。新广告额外传入 `creative_info.video_info.file_name`；历史冻结请求不重写。

封面沿用已有行为：使用视频封面 URL 上传图片取得目标账户图片 ID；已有目标图片 ID 优先复用。图片 URL 不直接冒充 `image_info.web_uri`。源图片 ID 不直接复制成目标图片 ID。

URL 原始导入的上传回执及异常恢复查询完整正文由独立加密归档保存；业务状态只保留必要标识和恢复字段，不公开签名 URL/令牌。正常源 URL 上传成功取得实际 VID 即完成入库，不再统一强制回查；共享需要获取目标实际 VID，不能照搬上传回执流程。

## 部署与验证

见[部署手册](../runbooks/deployment.md#素材原生共享与应用-mini-配置2026-09-13)。共享没有额外功能开关。新增 `materials.share_assets` 的租约需覆盖 900 秒硬限；连接工具合同更新须用原授权重新核实工具目录。数据库迁移为 `material_source_bc`，源外键/路由与目标分离且保留租户约束。

以下为历史离线记录，只说明当时版本的测试覆盖，不代表当前真实账户成功或当前分发路径。

## Offline validation

Behavioral tests use real local PostgreSQL and Redis, the pinned official SDK serialization/deserialization, `urllib3.PoolManager.request` as the TikTok transport double, and botocore `Stubber` for S3. They do not certify live advertiser permissions, actual video acceptance, cross-account sharing, or current tenant asset availability. Live validation needs a separately authorized concrete short video and source account.

2026-09-09 offline result: **118 tests passed** for `tests/modules/materials` plus `tests/contracts/test_tiktok_sdk_surface.py`, including **39 Task 3 tests**. This run used the separate local `tiktok_material_sdk_test` PostgreSQL database, isolated Redis keys, the real Task 2 storage adapter with botocore Stubber, and official SDK HTTP transport doubles. The only warning was the existing FastAPI/Starlette `httpx` test-client deprecation. Strict backend mypy (`--follow-imports=silent`), ty with the worktree Python environment, Ruff and Python compilation passed for the three Task 3 production modules. No live acceptance claim follows from these results.

Task 4 and review follow-up, 2026-09-09: **171 tests passed** for `tests/modules/materials`, `tests/contracts/test_tiktok_sdk_surface.py` and `tests/core/test_runtime_config.py`. This includes 33 readiness/distribution/concurrency tests plus the generated native-share wire contract; source review regressions cover actual PostgreSQL pre-send/finalization deadlocks and recoverable initial revocation. A source/target race uses the common send authority and emits one upload; a consumed read successor after actor revocation is repaired under the original revision and completes with one GET after authority returns. Strict mypy, ty, Ruff and compilation passed for the seven affected material production modules. No live TikTok or S3 writes were made.
