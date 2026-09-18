# 2026-09-18 批量共享链路加固与新加坡测试环境发布

## 结果

- 代码提交 `6f610779d9cb4c6d16e03683b667fcc3c8081013` 已推送至 `origin/main`，并作为固定版本部署到新加坡测试环境；生产环境未变更。
- 数据库由 `ad_same_group_reissue` 升级到单一 head `outbox_payload_compaction`。新增批量共享候选部分索引和已结算 Outbox 压缩字段/部分索引，迁移后 `alembic check` 无新增操作。
- 原批次 `454cfb90-d81d-4c81-b94c-9abbd5e00b42` 的业务投影实测为 `COMPLETED`，Campaign / Ad Group / Ad 成功数为 180 / 180 / 360，失败及未知业务对象均为 0。

## 修复范围

- 视频共享候选不再加载最多一万条 ORM 记录。锚点账户限定最多20个素材、锚点素材限定最多10个账户，再取最多200条交集；大范围查询使用专用部分索引。
- 同范围并发 worker 在锁前确实见到可领取锚点、但等待期间被前一批领取时，可接手同冻结范围下一批。迟到旧消息仍在加锁前退出，不会扩大授权范围或重放远端请求。
- 提交目录的历史 READBACK 可见状态、筛选、当前页、业务对象计数和总数合并为一条 SQL；历史步骤不删除、不改写。
- Outbox 不执行七天直接删除。只有发布满七天、且没有活动执行步骤/单元持有的高频构建消息才清空可重放 payload；投递 ID、task key、SHA-256 配置摘要和时间证据保留。素材准备/核查消息不参与压缩。
- 无消费者的单条 `result_unknown` 素材操作继续保留真实未知事实及重复发送栅栏；它没有当前分发消费者，也不进入恢复扫描，因此不阻塞原批次。

## 发布前验证

- 共享、万级候选、并发领取、Outbox、历史回执和提交目录：112项通过。
- 封面/视频补发、BC seed 恢复及核实替换：54项通过。
- 修改文件 Ruff、格式、ty 和 `git diff --check` 通过。
- 全新临时 PostgreSQL 从空库升级到 `outbox_payload_compaction`，`alembic check` 返回无新增操作。
- 无配置模型、环境模板、Worker 服务定义或功能开关差异；沿用已确认的素材导入/清理、调用策略、媒体白名单及并发值。

## 备份、恢复与迁移

- 完整备份：`/var/backups/tt-ada-staging/20260918T144836Z/`。
- `postgres.dump`、`redis.rdb`、`config.tar.gz`、`project.tar.gz`、`runtime-config.tar.gz` 均通过 SHA-256 校验。
- 项目归档隔离恢复核对1,396个文件；私有环境、MCP注册文件及Nginx站点逐字节一致。
- Redis RDB 在独立 Unix socket 实例装载、PING、DBSIZE及键读取通过。
- PostgreSQL dump 恢复至独立临时库后，先核对旧 head，再升级到新 head并运行 `alembic check`；22张业务表摘要及1,845份加密响应在迁移前后保持一致。
- 第一次发布尝试在备份前安全退出：历史校验脚本按文件路径启动时加载了共享虚拟环境的旧 editable 源码。未迁移、未切版，钩子恢复旧服务和备份 timer。改为从新 release 工作目录通过标准输入执行后，完整流程重新开始并通过。

## 线上验收与观察

- API、resources、resource-results、builds、control、Beat 六服务均为 active，`NRestarts=0`；四个 Worker 节点 ping、registered、active、reserved 均正常。
- 正确健康入口 `/api/utils/health-check/` 返回 `true`；仓库 `check-bootstrap.py` 的健康、构建登录、回调错误边界及受保护 API 检查通过。
- API、所有 Worker 和 Beat 均加载 `/etc/tt-ada-staging/app.env`；素材导入和自动清理保持开启，调用策略及长请求租约检查通过。
- 观察期到期 Outbox 持续为0，未来消息2条；发布后没有 `attempts>1` 的重新发布，没有任务重试、ERROR或Traceback。定时压缩任务已注册；当前尚无满七天的合格消息，因此压缩数为0。
- 原始历史中仍保留 READBACK FAILED 86、UNKNOWN 13、PENDING 107及一个已由核实替换收敛的原 UNKNOWN 广告；这些是审计行，不参与当前业务投影或执行窗口。

## 清理与边界

- 验收后删除隔离恢复临时数据库和目录。按用户此前要求删除旧备份 `20260918T132252Z`，当前只保留完整备份 `20260918T144836Z`；旧 release 目录未删除。
- 本次没有创建或修改新的 TikTok 广告，也没有更改新骏伯 BC 的授权、账户、素材或投放状态。现有完成批次用于只读回归确认。
- 备份仍位于同一主机，未配置异地副本。
