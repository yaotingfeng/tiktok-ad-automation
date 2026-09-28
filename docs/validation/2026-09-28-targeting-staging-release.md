# 2026-09-28 受众定向及本地累计改动——新加坡测试环境发布

## 发布范围

- 用户明确授权将本地其他修改一并推送并部署测试环境；12 个累计提交已推送 `origin/main`。固定运行提交 `ec45538eedd7aeee7f43156525555f5921c538d9`，替换服务器原 `1cfc603246f60ba961631cf04476492d97e1d9fa`。
- 纳入账户搜索选择器、身份/小程序目录选择改进、策略及批次严格定向、共同国家目录、冻结与回读合同。旧服务已包含部分累计提交，本次以固定完整版本统一交付。
- 数据库 `draft_identity_selection → audience_targeting`；无依赖锁或功能开关变化，原环境文件保持。用户已知首次无核实数据时“指定国家”无选项，独立地区获取入口尚未实现；本次没有把这一限制描述为已解决。

## 本地验证与发布产物

- 定向、预览、SDK 编译、执行及策略回归 173 项通过，追加 BC 隔离 1 项通过；策略/搭建页面 113 项及追加只读/摘要 2 项通过。
- 发布前追加双通道场景、场景合同及目录准备回归 66 项通过；固定版本 TypeScript/Vite 构建通过。相关 Ruff、ty、Biome、客户端生成和差异检查此前通过。
- 通过 Git archive 传输固定版本跟踪源码，不包含本地环境/SSH 信息。服务器对 1,261 个跟踪文件校验摘要；前端使用该提交在本机生成的生产静态产物，服务器校验完整包摘要。Python 依赖锁与原运行版本相同，沿用原可重建虚拟环境；候选版本模块导入、额度与租约检查通过。

## 停写、备份与迁移

- 四个 Worker 的 active/reserved 均为 0；停止 API/Beat、正常停止四个 Worker，并暂停备份 timer。没有删除队列、Outbox 或业务状态。
- 完整备份 `/var/backups/tt-ada-staging/20260928T101508Z/` 包含 `postgres.dump`、`redis.rdb`、`config.tar.gz`、`project.tar.gz`、`runtime-config.tar.gz`；五份 SHA-256 校验通过，私有配置覆盖 systemd、Nginx、证书、MCP 注册和备份脚本。
- 项目独立解压核对 1,330 个文件和实际前端产物；Redis 在独立 Unix socket 实例载入并读取成功。PostgreSQL 在新建隔离库恢复后，22 张业务表原有字段的摘要保持一致，10,484 份加密响应全部验证解密、长度及 SHA-256，既有连接凭据可解密。
- 恢复库迁移演练、`alembic check`、迁移后历史摘要验证通过后，才对测试实例迁移；正式库 current/head 为 `audience_targeting`，无新增模型差异。比较历史数据时仅排除新增的 `targeting_override` 列，未更改旧数据。
- 独立恢复的临时库和目录验证后清理，原版本和完整备份保留；同机备份仍不等于异地备份。

## 运行与页面/API 验收

- `current` 与 API、Resource、Result、Build、Control、Beat 全部实际进程目录为固定新版本；11 个进程实际配置摘要发布前后一致。
- `MATERIAL_INGEST_ENABLED=true`、`MATERIAL_CLEANUP_ENABLED=true` 保持；调用策略、媒体白名单、数据库/Redis、加密及集成配置保持，`app.env` 摘要未变。
- 六服务 active、NRestarts=0；四个 Worker 已响应并核对进程池并发为 2/1/1/1，resources/resource-results/builds/control 队列均为 0，备份 timer 已恢复。发布后检查 warning/error 日志无条目。
- HTTPS 健康、登录 HTML/资源、两类真实管理员登录、profile、租户访问及平台管理权限隔离通过；MCP 配置仍 READY，仅表示配置状态。
- 新版 OpenAPI、未登录定向接口 401、已有策略默认定向、本地 BC 参考地区与已有草稿定向读取通过；未调用 TikTok/版权方接口，未创建、重试或启用广告。真实广告定向效果不在本次验收范围。
- 验收脚本曾因无序媒体白名单的字符串顺序产生误报，改为规范化排序后通过；切换后首次进程数检查早于 prefork 池启动，等待就绪后重核 11 进程及各池并发通过。OpenAPI 将策略模型分为 Input/Output，验收按实际模型名称修正；以上均为运维校验修正，应用提交未改变。

## SSH 复用

用户授权的 SSH 信息仅保存于本机 `.runtime/singapore-staging/ssh.json`（0600）；辅助入口及使用方法已登记环境手册，不包含密码。三份本地连接文件均由 Git 忽略，未进入源码包、提交或服务器发布配置。
