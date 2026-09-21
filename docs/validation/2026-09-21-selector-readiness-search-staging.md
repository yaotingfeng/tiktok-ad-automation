# 2026-09-21 选择器提前可用与搜索——新加坡测试环境验收

## 范围

- 目标：新加坡 staging，仅星屿租户的通用搭建页面行为；代码仍保持所有租户/BC 隔离。
- 变更：投放身份和小程序目录按各自资源完成状态提前展示；两个选择弹窗增加缓存目录搜索。
- 不变：CTA、VBO、地区校验、首账户参考规则、逐账户身份/小程序复核、草稿 READY 写入门槛、正式广告创建边界。
- 无数据库迁移、依赖或部署级开关变化；沿用 staging 已确认的私有配置、调用策略和素材开关。

## 根因证据

测试服星屿近期 5 个成功场景任务的资源时间显示：identity 在任务开始后约 8–16 秒落库，Minis 在约 17–33 秒落库，完整任务在约 70–86 秒结束。原目录查询要求 `SceneJob.status=COMPLETE`，因此已经落库的目录仍被 CTA、VBO、地区读取阻塞。

## 本地验证

- TDD RED：PENDING 场景中的 identity/完整 Minis 目录均返回 pending；两个弹窗不存在搜索输入。
- TDD GREEN：资源提前可用 3 项、缓存搜索 2 项；目录/预览相关真实 PostgreSQL/Redis 39 项通过。
- 前端准备页 Playwright 67 项通过，覆盖搜索、跨页归零、刷新保留结果、准备中只读浏览和保存门槛。
- 整包复审发现并修复“投放身份搜索无结果后关闭弹窗，入口被过滤结果误禁用”的边缘问题；新增后端状态和前端关闭重开回归先失败后通过。最终目录/预览 39 项、准备页 67 项再次通过。
- 改动 Python 文件 Ruff、改动前端文件 Biome、TypeScript/Vite 生产构建、OpenAPI 客户端生成和 `git diff --check` 通过。
- 完整 builds 套件结果为 597 passed / 3 skipped / 6 failed / 207 errors。207 项为上一轮身份目录上线后旧 synthetic preview fixture 未保存身份目录；其余既有失败涉及 identity/minis 分页 fixture、历史迁移 head 和 PostgreSQL planner。该基线债务单独保留，不计作本轮全绿证据。

## 部署与服务器验收

- 最终固定提交 `1cfc603246f60ba961631cf04476492d97e1d9fa` 已部署到新加坡 staging；`current`、API、Resource、Result、Build、Control 和 Beat 的实际 cwd 均指向该目录。依赖锁文件与旧版本一致，服务器使用 Python 3.14.2、Bun 1.4.2 完成候选检查和前端生产构建。
- 两次切换前四个 Worker 均无 active/reserved 任务；备份 timer 暂停，API/Beat 停止后四个 Worker 正常停止。首次切换备份批次为 `/var/backups/tt-ada-staging/20260921T022141Z/`，PostgreSQL、Redis、私有配置/证书和旧项目四份归档的 SHA-256 均通过。
- 复审修复的最终切换重新执行完整停写备份，最终批次为 `/var/backups/tt-ada-staging/20260921T023539Z/`。PostgreSQL dump 在隔离库恢复出 107 张表和 5,142 份加密响应，抽样正文可用原密钥解密且长度、SHA-256 一致；Redis RDB 在独立端口 PONG 并载入 keyspace；项目归档独立解压核对 1,662 个文件及实际前端产物，私有配置、systemd、Nginx、证书和备份脚本可独立解压。临时库与目录已清理，两次正式备份和旧 release 均保留；同机备份仍未配置异地副本。
- 数据库 current/head 前后均为 `draft_identity_selection`，无迁移。`MATERIAL_INGEST_ENABLED=true`、`MATERIAL_CLEANUP_ENABLED=true`、调用策略、媒体白名单和加密配置保持，发布前后 `app.env` 摘要一致。
- 发布后六个服务均 active、`NRestarts=0`，四个 Celery 节点均 pong，备份 timer 已恢复。Bootstrap 通过健康页、生产登录页、回调业务错误边界和受保护 API 边界；四个队列均为 0，发布后 warning/error 日志为空。
- 线上 OpenAPI 的 identity/minis GET 均包含可选 `query`；生产前端产物包含“搜索投放身份”和“搜索小程序”。使用平台管理员对星屿旧草稿执行 `%_` 的只读缓存搜索，两个接口均正常响应，验证特殊字符按普通查询值处理；没有创建新准备任务。
- 本次发布没有调用 TikTok 接口，没有创建、重试或启用广告。由于没有人为启动新的场景任务，线上未伪造一个“进行中”的提前可见样本；该时序由真实 PostgreSQL/Redis 回归覆盖，线上仅验证已缓存目录查询、OpenAPI 和前端产物。
