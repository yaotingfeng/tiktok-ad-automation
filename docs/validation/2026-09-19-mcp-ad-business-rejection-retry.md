# MCP 广告业务拒绝有限重试验收

## 范围与安全边界

- 本次只调整官方 MCP 的 `build.create_ad / smart_plus_ad_create`。根据新骏伯两个批次的实际证据，将 `40002`、`51002` 两个完整非零业务回执列入“平台明确拒绝且没有创建对象”的窄白名单；其他操作、其他业务码、畸形回执、连接中断和无响应继续保持 `UNKNOWN`，禁止自动重放。
- `40002` 最多自动补 1 次，`51002` 最多自动补 2 次，分别使用 5 秒、5/10 秒退避。重试复用原冻结正文、摘要、attempt ID、父级和路线，不改广告名，不新建恢复代数；预算耗尽后落为 `FAILED`，不会无限调度。
- 每个 nonce 必须具备完整的 `REQUEST_ARMED → REMOTE_REJECTED` 或原有 `REQUEST_ARMED → NOT_SENT` 证据。出现 `CREATED`、`LATE_CREATED`、`RESULT_UNKNOWN`、armed 租约过期、远端 ID 或证据损坏时，候选 SQL 和领取逻辑都会拒绝重放。
- 仍不保存或输出上游 `message`，避免令牌、签名 URL 或其他私密原文进入普通错误、日志及状态；诊断保留经过整数校验的业务码、request ID、正文摘要和拒绝次数。

## 本地验证

- 解码/状态/安全恢复专项：133 项通过。
- MCP 真实本地协议传输链验证：第一次 `40002` 落为 `PENDING/REMOTE_REJECTED`，第二次沿原正文创建成功；两次调用、attempt ID 和正文摘要保持一致，私密 message 未进入证据。
- 相关 MCP 结果、传输、适配器、执行状态、安全恢复、交付和双通道执行组合：312 项通过；另有 1 个基线用例 `test_pending_cover_waits_without_repeated_step_or_unit_messages` 失败。失败路径位于本轮未修改的封面 READY 依赖直接结算测试，单独复跑仍失败，不归入本功能通过数，也未用它掩盖本轮结果。
- Ruff、格式检查、ty 检查及差异检查通过。测试全部使用本地专用 PostgreSQL、Redis 和 HTTP 传输替身，没有调用 TikTok 或创建真实广告。

## 测试环境发布

- `1e9ca7a221988d172e8f99f75d43b552e5a87c7f` 已推送并部署到新加坡测试环境。数据库无迁移，head 保持 `draft_scene_phase`；`uv.lock`、`bun.lock`、功能开关、调用策略、调度预算和低配机 Worker 并发均保持不变。
- 第一轮备份 `/var/backups/tt-ada-staging/20260919T065024Z/` 已完成五份归档校验、项目/私有配置/Redis/PostgreSQL 独立恢复和迁移演练，但候选 release 顶层目录继承本机归档的数值 owner 与 `0700` 模式，`tt-ada` 无法进入。脚本在切换前退出，`current` 未变；数据库没有新迁移，旧六服务随即恢复。修正候选顶层为 `root:root 0755` 并以服务用户预检后重新发布。
- 有效切换备份 `/var/backups/tt-ada-staging/20260919T065212Z/` 包含 PostgreSQL、Redis、旧项目及前端、私有配置/systemd/Nginx/证书/备份脚本和校验清单；隔离恢复核对 1,319 个文件、22 张表、2,705 份加密响应，Redis PING/读取和恢复库迁移演练均通过。两份 59 MiB 完整备份均保留，本机尚无异地副本。
- API、Resource、Result、Build、Control、Beat 六服务的实际 cwd 均指向新 SHA，全部 active、`NRestarts=0`；4 个 Celery 节点 pong，HTTPS bootstrap 通过，backup timer 已恢复。服务器专项 12 项通过；专用测试角色随后恢复 `NOLOGIN NOCREATEDB`，临时数据库已清理。
- 发布后四个业务队列均为 0，日志自切换后没有 WARNING、ERROR、CRITICAL、Traceback、WorkerLost 或 OOM。发布没有调用产品 `RETRY/RECONCILE`、没有调用 TikTok 写接口，也没有新增真实广告；两条历史未发布原件核验 Outbox 保留，未被清理或强行执行。
