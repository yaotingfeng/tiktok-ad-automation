# 2026-09-13 素材共享与应用配置验收

## 范围

用户要求同 BC 使用素材共享、跨 BC 使用 URL 上传，官方 API 与服务器官方 MCP 行为一致；补齐 Mini 配置页面及原文件名。沿既有授权推送并发布新加坡测试环境。封面流程不在本轮变更范围。

同 BC 通过源视频 MID 调用 `creative_asset_share`（官方 MCP 工具 `creative_asset_share_get`），共享成功回执本身不含目标 VID，按目标名称/摘要定位真实 VID 并落库。跨 BC 分别冻结源和目标授权，不能把目标 BC 套在来源素材上。超时/未知只核实，不自动改成重新上传。

原名经过路径/控制字符清理，100 UTF-8 字节内保留首尾和扩展名；上传附 8 位关联码，广告视频信息使用原名。共享由平台复制来源名，历史已被旧代码重命名的源资产不会凭空恢复远端名称；新建广告仍可使用本地保留的原名。

## 本地验证

- PostgreSQL/Redis 使用独立测试库及 DB 13/14；外部边界为模拟传输，不等同真实 TikTok 验收。
- API/MCP 端到端来源上传 → 原生共享 → 封面 → 预览 → 创建及严格回读，以及原生共享/跨 BC 隔离用例最终重跑 8 passed。
- 最终相关分发、跨 BC、文件名、创建素材及整个 TikTok 集成目录回归：493 passed、1 skipped；版权方页面浏览器测试 22 passed，包含保存后显示 Mini 与非管理员隔离。
- 后端 12 个实现文件 mypy、Ruff、diff 检查通过；前端 TypeScript/Vite 构建通过。
- 较早扩大回归出现旧测试预期未同步及本机默认 ingest/S3 环境问题；本轮相应用例已按真实跨 BC 或原生共享路径修订。未修改分支基线 `8300b3b` 的封面/上传未知状态测试，在单独恢复的基线代码与数据库仍有 16 failed、80 passed，故不宣称仓库全量历史套件全部通过。

## 发布与真实验收

- 运行版本 `b89ad1f3aa9cb1d94a77d942bf52ace3261baa4d`，已推送 `origin/feat/platform-implementation`；同分支前置策略命名提交随版本包含，本轮不覆盖它。数据库 head `material_source_bc`。本记录后续文档提交不改变服务器代码版本。
- 最终完整备份 `/var/backups/tt-ada-staging/20260913T060649Z/`：PG、Redis RDB、项目及构建、私有配置/证书五个归档摘要通过；临时 PG 库恢复后以应用角色演练新迁移成功，素材响应可解密且长度/摘要一致，独立 Redis 装载 PING/读取通过，项目/配置隔离解压比对通过。新源码及前端 1100 个文件摘要一致。
- 第一次演练因 postgres OS 用户不能执行应用解释器而中止，未迁移业务库、未切换版本，旧服务和备份 timer 自动恢复；改为应用角色拥有的独立恢复库演练后，重新备份发布。未扩大解释器目录权限或删除业务数据。
- API 1 个、Worker 3 个、Beat 1 个进程实际 cwd、数据库/Redis/加密配置/MCP 注册/配额一致。`MATERIAL_INGEST_ENABLED`、`MATERIAL_CLEANUP_ENABLED` 发布前后均 true；原精确媒体主机保持。无新功能开关，沿用既有用户授权。健康、构建登录页、OAuth API 边界检查通过，备份 timer 恢复 active。
- 使用服务器自身官方 MCP 连接，已有授权重新 tools/list、刷新 BC 目录及绑定 BC 同步完成；随后走正常角色重检任务，目标账户恢复真实 VERIFIED、can_upload/can_build=true。没有修改权限位或授权材料，没有使用本地 TikTok MCP。
- 骏伯 BC `7678608005688066065`，服务器连接 `90d01aa0-ab41-43e2-ad33-11c44b2da73a`。用户指定目标账户 `7680027514155319304`，从同 BC 已有来源账户 `7680028557046611986` 原生共享一条素材。分发 `9ada1ab9-0d9a-48f9-9732-0490cb400cba` 最终 ready，操作 succeeded，`transport=native_share`、`share_acknowledged=true`，无错误；取得目标 VID `v10033g50000dah81ifog65qua2k56k0`、MID `7683845391322906644`。未走 URL 上传、未新增广告。
- LemonShow 的现有应用 `com.lemonshow.newdrama.ttminis` → `mnlb1cmig5uuc1nq` 已保留。页面管理员新增入口的保存/刷新经过模拟浏览器验证；本轮未改线上既有映射。
- 真实业务验收范围是服务器官方 MCP 同 BC 共享。API 同 BC、双通道跨 BC、文件名传参与广告创建回读由真实 PG/Redis 加传输模拟验证，不冒充新的真实广告创建或跨 BC 现场测试；此前成功广告批次保持 COMPLETED。
