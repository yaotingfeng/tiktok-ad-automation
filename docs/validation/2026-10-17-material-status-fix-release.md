# 2026-10-17 素材启停修复及生产发布

## 变更

- 素材启停前按完整业务身份查询，修复把六字段身份误当 UUID 主键的异常；预览与执行共用查询并保留租户/账户/广告范围。
- 确定未调用平台时的校验异常持久化为 NOT_SENT，保存失败原因、回执和领取释放；不可重试项不再永久 RUNNING。平台发送结果未知仍保持 UNKNOWN，不自动重放旧任务。
- 保持报表素材展示方式不变；无迁移和配置开关变化。

## 本地验证

- 红灯：新增启用/暂停及发送前异常 3 例，分别复现 InvalidRequestError 和异常未收尾。
- `pytest tests/modules/ad_management -q`：108 passed。
- `pytest backend/tests/modules/ads backend/tests/modules/automation_rules -q`：278 passed。
- 相关 Ruff 检查、格式检查、ty 检查及 `git diff --check` 通过。
- PostgreSQL 18 与 Redis 8 为本轮独立本地容器；仅替换外部网关边界，未调用真实 TikTok 写入。

## 生产发布

用户授权修复并发布。目标为新加坡 ACK 的 `test` 命名空间（生产用途），域名 `https://tkada.shortclipflix.com`。旧版 `cbc57a38fc7dfb455a678617c0e16456f0d2cda6`，候选为 `63975325c5e0f8292b4a9f5c23abfe783a987520`。四个业务开关沿用线上实际值，不新增或重置配置。

状态：生产发布完成。公司 Jenkins [#15](http://123.207.4.46:8080/job/tiktok-ad-automation/15/) 由本轮公司 main 推送自动触发，2026-10-17 14:42（北京时间）结束为 SUCCESS；批次 `20261017T062148-46618c`。

- 发布门禁：affected 范围 7 个改动文件、20 个后端测试文件，后端检查线通过（28 秒）；构建/上传/备份/恢复/切换/验收通过。
- 同一停写窗口备份：PostgreSQL、Redis、Beat 状态、独立源码/应用与前端构建、配置及证书引用齐全，归档校验并真实隔离恢复通过。最新备份位于 ACK PVC `tiktok-ad-automation-backup` 的 `/backup/latest/`。
- 本轮恢复证明绑定清单 SHA256 `31348444cba0a6e391466ccde0d6dd2564350c0f36d430b4dbf08973755f356b`；`postgres_matches=true`、6 份凭据解密通过、Redis 14 键验证/重新编码通过，候选 SHA 为 `63975325…`。
- 九 Pod 全部 Ready、实际镜像均为 `63975325…`；逐 Pod 从 Settings 读取四个业务开关均为 true，与发布前一致。迁移仍为 `0036_drop_rule_safety_settings`。
- HTTPS `/api/utils/health-check/` 返回 200 / true；发布辅助 Pod 与独占锁已清理。
- 用事故广告的两个当前广告内素材记录，在生产只读事务中执行 `_same_frozen_value` 和指令构造，均通过；启用/暂停状态分别保持 ENABLE/DISABLE。未打开平台网关、未更改素材状态、未重放历史卡住任务。真实 TikTok 启停写入不属于本轮自动验收。
- 私有本机核验脚本与九 Pod/健康证据保存在 `.runtime/material-status-fix-20261017/`，未提交业务记录或私密配置。
