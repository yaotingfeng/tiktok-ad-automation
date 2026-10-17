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

用户授权修复并发布。目标为新加坡 ACK 的 `test` 命名空间（生产用途），域名 `https://tkada.shortclipflix.com`。旧版 `cbc57a38fc7dfb455a678617c0e16456f0d2cda6`，候选为本轮聚焦提交。四个业务开关沿用线上实际值，不新增或重置配置。

状态：待公司 Jenkins 发布门禁、发布前一致性备份及隔离恢复、九角色切换和生产只读核验。真实素材启停写入不属于本轮自动验收。
