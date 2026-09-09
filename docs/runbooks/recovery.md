# 任务恢复与数据恢复

## 页面入口

在正确租户和 BC 的任务详情中查看三层对象、素材目标账户映射和步骤事件。以下操作保持冻结内容和原提交人的当前权限检查：

| 情况 | 入口与结果 |
| --- | --- |
| 明确未发送且依赖已恢复的失败步骤 | 重试失败；服务端重新检查是否有可重试项 |
| 创建结果未知、已有 ID 待核实、参数不一致 | 核查结果；只查询远端，不能凭查不到重新创建 |
| 改预算、素材、文案或目标账户 | 返回搭建流程，生成新的预览并审阅提交 |
| 点击操作后网络断开 | 使用保存的原 request ID 回查；不要换 ID 猜测重发 |

恢复 POST 返回的是持久化受理回执；原请求 GET 始终保留初始受理结果。恢复任务 GET 返回当前扫描进度，`COMPLETED` 表示恢复扫描和调度已完成，具体广告结果仍看原任务详情。成功对象保留原 ID；核查不会自动启用、暂停或删除远端对象。

素材 UNKNOWN 经原上传操作的严格只读核实路径处理。历史分发记录显示 ready 但目标 VID/封面证据已过期时仍需核实，不会重新上传。目标映射、实际目标账户和当前连接均匹配后才继续广告步骤。

## 运行故障

Redis 暂时不可用时，数据库 outbox 保留未完成投递；准入失败发生在 SDK 调用前。恢复 Redis 后确认单一 Beat 和 Worker 正常，等待原消息退避到期。不要清空 Redis、outbox、ExecutionStep 或请求回执来清除错误。

Worker 异常退出后，已进入请求发送阶段的步骤按 UNKNOWN 核查。尚未发送的步骤可在租约到期后重排；实际请求使用硬期限，额度租约覆盖进程退出，防止旧请求尚未结束就释放并发额度。检查当前版本是否包含对应修复，再等待控制队列的周期修复；不要把 RUNNING 批量改成 PENDING。

连接、成员或租户被停用后，未执行步骤重新检查权限并阻断。已启用的远端广告继续保留原状态。修复连接应从租户授权入口重新授权，再在任务详情核查/重试符合条件的项目。

## 观察位置

任务列表支持状态、时间、版权方和搜索；任务详情区分三层成功/失败/未知与辅助素材/CTA阶段。账户与连接页展示当前权限和授权状态；重试按钮是否可用以当前服务端结果为准。

运维侧在只读数据库连接观察 `pending_dispatch` 未发布条数、最早到期时间和 attempts；通过 `(available_at, id)` 分页查看样本。`execution_step` 的 status/error_code/due_at 显示待办和原因，`step_evidence` 保存发送与回读证据。避免把完整请求 JSON、链接、账户明细批量导出到日志。租户等待情况可结合 outbox 最早到期时间及 `dispatch_tenant_cursor.last_published_at` 判断；容量报告中的公平性指标是合成场景实测，不代表平台 QPS。

在部署主机使用相同 Compose 文件查看状态：

```bash
docker compose -f compose.yml -f compose.staging.yml ps
docker compose -f compose.yml -f compose.staging.yml exec -T worker celery -A app.jobs.celery_app:celery_app inspect ping
docker compose -f compose.yml -f compose.staging.yml logs --tail 100 worker beat
```

## 备份与恢复

备份必须包含 PostgreSQL、原件对象存储和独立保管的 `CONNECTION_ENCRYPTION_KEY`。数据库备份中的密文凭据需要同一加密密钥才能解开。保存备份完成时间、应用 SHA/镜像 digest、Alembic head、对象备份位置；归档保存在受控存储，不提交到 Git。

以下备份命令只写本地受控文件；目录权限由 umask 限制。先按发布流程停止新提交并排空执行 Worker，保留 Redis 持久卷。

```bash
umask 077
mkdir -p .runtime/backups
docker compose -f compose.yml -f compose.staging.yml exec -T db pg_dump -U postgres -d app --format=custom --no-owner --no-acl > .runtime/backups/app.dump
```

先恢复到独立的新数据库并隔离消费者，不直接覆盖业务库。使用与服务端主版本匹配的 pg_restore，通过受控 PG 环境变量提供连接配置，执行 `pg_restore --no-owner --no-acl --exit-on-error --dbname "$RESTORE_DATABASE" .runtime/backups/app.dump`。核对迁移 head、关键记录、原件对象和凭据解密后才评估恢复上线。

旧备份可能缺少备份之后真实成功的远端写入。此时数据库中的 QUEUED 或空 ID 不能证明远端不存在；保持 Worker/Beat 停止，先结合受控运行记录和远端只读查询核对整个缺失时间窗，保留找到的对象 ID。无法唯一核实的项目保持人工待处理，不允许自动恢复创建。普通 retry/reconcile 只认识数据库里已有的事实，不能自动补出备份丢失的发送记录。

可重复的离线演练（backend 目录，显式设置专用 `*_test` 数据库环境）：

```bash
uv run python -m scripts.rehearse_backup --output ../docs/acceptance/backup-restore.json
```

脚本仅创建和删除自己生成的两个合成数据库，使用实际 pg_dump/pg_restore，核对全表行数、迁移版本、合成密钥解密、素材对象元数据和待投递记录。它不启动 Worker、不接触对象存储内容，也不证明业务备份时间窗中的远端写入已恢复。当前实测结果见 [backup-restore.json](../acceptance/backup-restore.json)。
