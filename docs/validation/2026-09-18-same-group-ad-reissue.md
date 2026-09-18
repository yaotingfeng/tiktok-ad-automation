# 2026-09-18 新骏伯 UNKNOWN 广告同组补建验收

## 范围与授权

- 环境仅为新加坡测试服 `137.220.150.31:22211`；BC 为新骏伯，冻结连接 `f4f76aba-da22-4d5c-a123-3643500ec75b`。
- 原提交 `454cfb90-d81d-4c81-b94c-9abbd5e00b42`，原 UNKNOWN AD 步骤 `43720e9a-158b-599a-8e43-61435797a6b1`。用户明确要求重建并接受可能重复广告的风险；未授权重建其他广告或修改素材、预算、账户和父级结构。
- 原请求摘要 `00ff30c82ac9c14f76d508f2c34a7a0fc54c77b1266c3ad2654187ab5178acce` 保持不变。创建前再次完整读取原组，只有 `sp1` ID `1876624547889377`，原 `sp2` 与补建名均不存在，原组状态为 ENABLE。

## 代码与验证

- `d391dec046eef7ab1660fba3fda1a2354bf68ae9` 扩展既有 `build_verified_replacement`：另建组仍要求旧组无成功广告且 DISABLE；明确批准的同组单条补建允许保留成功兄弟，只允许广告改名，组配置必须完全一致且为 ENABLE。
- 新迁移 `ad_same_group_reissue` 仅放宽补建台账的“新旧组 ID 必须不同”约束；其它唯一性、不可变触发器和原 UNKNOWN 保护保持。
- 测试先观察新增同组用例因 `replacement_intent_mismatch` 失败，修复后 `test_verified_replacements.py` 14 passed；Ruff、ty、Alembic 单 head 通过。组合回归 57 passed / 1 个既有分页查询计数断言失败，独立复现且不涉及本轮文件。

## 发布

- 发布前正常排空，`20260918T132252Z` 的 PostgreSQL、Redis、私有配置、证书和项目归档校验通过；1,395 个项目文件逐字节恢复、Redis 独立实例读取、22 表/1,767 份加密响应及恢复库迁移均通过。
- 实际版本为 `d391dec046eef7ab1660fba3fda1a2354bf68ae9`，数据库 head 为 `ad_same_group_reissue`。六个 systemd 服务 active，4 个 Celery 节点 pong，HTTPS bootstrap 检查通过；前端继续使用上版构建，配置、额度、并发和功能开关未变。
- 用户要求删除旧备份；清理后仅保留最新完整且含 `RELEASE_COMPLETE` 的 `/var/backups/tt-ada-staging/20260918T132252Z/`，删除不可恢复。

## 真实补建结果

- 唯一发送前将冻结范围、请求摘要、补建意图摘要和 attempt 写入 root 私有审计文件；若结果未知，命令会停止且不重复发送。
- 补建广告名为 `{b32823/s362977/c1}-Um Bebê Fora dos Planos-32823-20260917-sys-CF8H-g01-sp2-rebuild1`，TikTok 返回 ID `1876676311288962`。
- 应用按 ID 回读得到唯一广告、状态 ENABLE；按原组完整读取共两条：原 `sp1` 与新 `sp2-rebuild1`，父组状态 ENABLE。补建台账 ID 为 `4602d48c-3459-4098-9308-9c6736f2ae12`，原 UNKNOWN 行及其历史 READBACK 不改写。
- 最终业务投影：提交 COMPLETED；Campaign 180/180、Ad Group 180/180、Ad 360/360、MATERIAL 4770/4770；pending/unknown AD 均为 0，retry/reconcile 候选均为 0。Redis `resources`、`resource-results`、`builds`、`control` 四队列均为 0，最近服务日志未出现本轮 Traceback、死锁、WorkerLost 或 OOM。
