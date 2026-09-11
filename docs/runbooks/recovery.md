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

连接、成员或租户被停用后，未执行步骤重新检查权限并阻断。已启用的远端广告继续保留原状态。修复连接从租户授权入口处理；正常凭据轮换不改变授权版本，重新授权不会自动接管旧请求。旧授权任务按下述独立历史核查规则处理，不能直接恢复创建。

## 观察位置

任务列表支持状态、时间、版权方和搜索；任务详情区分三层成功/失败/未知与辅助素材/CTA阶段。账户与连接页展示当前权限和授权状态；重试按钮是否可用以当前服务端结果为准。

运维侧在只读数据库连接观察 `pending_dispatch` 未发布条数、最早到期时间和 attempts；通过 `(available_at, id)` 分页查看样本。`execution_step` 的 status/error_code/due_at 显示待办和原因，`step_evidence` 保存发送与回读证据。避免把完整请求 JSON、链接、账户明细批量导出到日志。租户等待情况可结合 outbox 最早到期时间及 `dispatch_tenant_cursor.last_published_at` 判断；容量报告中的公平性指标是合成场景实测，不代表平台 QPS。

在部署主机使用相同 Compose 文件查看状态：

```bash
docker compose -f compose.yml -f compose.staging.yml ps
docker compose -f compose.yml -f compose.staging.yml exec -T worker celery -A app.jobs.celery_app:celery_app inspect ping
docker compose -f compose.yml -f compose.staging.yml logs --tail 100 worker beat
```

长时间等待的广告会在发送前重新检查视频和封面。若广告本身从未发送，但封面上传结果未知，任务可显示本地失败；用户重试该广告时，系统先对原封面任务进行只读核查，已有 ID 优先 GET，不能直接重复上传。仍有歧义则保持待核实。已经成功的素材准备历史和已创建广告不会因此重开。

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


## 双通道冻结与重新授权

任务持久化 route 中的 channel、connection_id、authorization_revision 和 adapter_contract_revision 是原执行归属，不能由今日 BC 默认连接替换。来源素材沿 source_route 查实际源账户，目标素材/封面/广告沿父目标 route。远端返回的 VID、MID、image ID 与 Smart+ ID 分别保留，不用来源 ID 或请求账户填回目标事实。

- **凭据轮换**：credential_revision 变化本身不废任务；HTTP 前取得当前有效凭据但仍核实原 route 的授权与合同版本。正常观察刷新也不是新的授权。
- **刷新等待**：pending 保留原刷新操作及持久 outbox 等待；UNKNOWN 或要求重新授权时保留候选和回执，不重送旧 refresh token。重新授权、停用或失去成员权限优先于迟到 worker 的发布。
- **广告明确 NOT_SENT**：仅当前原 attempt 和 lease 有明确零发送证据、没有 UNKNOWN/已知 ID/迟到副作用时，才按原 body/request_id/attempt 安全重排。新 nonce 拒绝旧 worker 覆盖。看见任意历史 NOT_SENT 不足以放行。
- **广告 UNKNOWN**：只读核查原连接，不重新 create 或改通道；空列表、等待时间、网络恢复均不是未发生证明。已知 ID 先保存再关闭客户端，清理报错也不能重新创建。
- **新授权只读核查**：仅服务端证明同一租户/BC/连接/通道、同一授权主体及 issuer/resource、当前明确读取权限和完整原创建证据时，管理员可显式启动独立审计。审计保存新旧版本，不改原 route、步骤或正文，也不触发继续搭建。
- **核查回执丢失**：保留原 request UUID 并用 GET 恢复。PENDING/RUNNING、丢回执或 GET 404 不再 POST；已取得并验证原 read_id 的 UNKNOWN/BLOCKED 终态，当前管理权限与服务端资格恢复后，才显示“再次只读核查”供用户明确点击。

历史缺原 route/授权依据时保持 `legacy_route_unverifiable` 或对应缺证据状态，允许查看已有事实，但不可补今日默认或当前授权版本。场景/素材历史 NULL 也不能补写成今日连接。迁移应保存已有请求、摘要、已知 ID 和事件；有非空新证据时拒绝直接降级。

## 原件与封面未知结果

上传 UNKNOWN 是远端副作用未明，不能由 URL 到期、任务/Redis lease 到期、没有 VID、连接失效或等待足够久推断未上传。OriginalUse 持久保护必须持续；已知 VID 但摘要/大小/身份回读不完整也不释放用途。定时清理、孤儿清理、废弃会话和人工清理入口均不得绕开此保护。

来源或目标素材只能经原 route 的实际强回读确认，精确完成只释放当前操作用途；同一原件的其他用途继续阻止删除。迟到完成不得覆盖新 claim 或释放新 owner 的用途，数据库提交失败也不能留下“用途已释放但结果未提交”。

封面保存原请求视频 MD5 和真实图片回执 `receipt_facts`；原 MD5 不随今日素材变化，缺失的历史值不能补写。图片 ID 已知但详情未核实时只读恢复；actual receipt 已提交后即使 HTTP 清理失败也保留，不能从新查询或请求猜造旧回执。原 route、MD5 或唯一回执无法证明时停止发布映射与下游广告，不重复上传。

上述代码边界已有合成 PG/Redis/HTTP 用例，真实 MCP 服务字段、上游重试及 R2 清理仍需按 [真实联调表](../acceptance/live-mcp.md) 单独验证。


若升级后素材显示 `admission_policy_invalid`，先核对受控配置中 `TIKTOK_CALL_POLICIES.endpoints` 是否仍使用旧 SDK URL。当前按逻辑 operation 查询；旧视频上传 URL 覆盖须迁到 `materials.upload_video_file`、`materials.upload_video_url`，lease 严格大于 905000ms 才覆盖当前 900 秒任务及清理余量。其他网关操作按发布版 accounts/scene/materials/build/protocol 实际键逐项核对，保留共享 base/额度域；独立 MCP 刷新的 `auth_refresh` 键保持原名。不增加 URL fallback、不缩短硬期限或放宽保护来解除等待；测试用额度不能作为生产官方额度。配置问题解决后沿原消息/attempt 的安全状态恢复，UNKNOWN 仍只读核查。

本轮恢复与证据保护已纳入本地完整矩阵（2203 passed / 9 skipped），具体失败修复经过与版本见 [离线验收](../validation/2026-09-11-tiktok-dual-channel-offline.md)。8 项 Linux prefork 没有执行；本地合成回执和真实 PostgreSQL/Redis 不能代替目标 Linux 的进程终止或生产备份恢复验收。
