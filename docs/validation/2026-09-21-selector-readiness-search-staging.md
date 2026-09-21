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
- 改动 Python 文件 Ruff、改动前端文件 Biome、TypeScript/Vite 生产构建、OpenAPI 客户端生成和 `git diff --check` 通过。
- 完整 builds 套件结果为 597 passed / 3 skipped / 6 failed / 207 errors。207 项为上一轮身份目录上线后旧 synthetic preview fixture 未保存身份目录；其余既有失败涉及 identity/minis 分页 fixture、历史迁移 head 和 PostgreSQL planner。该基线债务单独保留，不计作本轮全绿证据。

## 部署与服务器验收

部署完成后补记固定提交、备份批次、独立恢复、服务/Worker、OpenAPI、前端和只读目录查询证据。验收不得触发 TikTok 请求或广告写入。
