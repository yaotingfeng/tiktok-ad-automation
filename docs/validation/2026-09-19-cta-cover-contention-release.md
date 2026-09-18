# CTA 期限与封面领取竞争修复发布验收

## 范围与结论

- 目标是新加坡测试环境、新骏伯 BC `7683817908149272592`，应用任务继续使用原冻结 `OFFICIAL_MCP` 连接；没有修改默认路由、授权、功能开关或调用额度。
- 发布 `f0e089da72eb11738095c93b20a2c70f217fdfb0`：只有明确 `NOT_SENT` 的 `tiktok_call_deadline_exceeded` 使用既有三次传输重试；封面批量同伴被合法领取时，未armed锚点延迟重排，不再终态BLOCKED。armed、UNKNOWN、摘要/权限/正文变化的安全栅栏保持不变。
- R627 与 6X3F 各执行一次用户明确授权的产品内 `RETRY`。恢复任务分别扫描并调度14/14、30/30项；这是恢复调度完成，不等于整批远端对象全部完成。

## 备份、恢复与切换

- 停止API接收写入、停止Beat并正常排空四个Worker；保留Redis队列，停机备份时`builds=150`。暂停备份timer，备份服务无在途执行。
- 完整批次 `/var/backups/tt-ada-staging/20260918T170055Z/` 含50MiB PostgreSQL dump、Redis RDB、6.9MiB独立项目归档、私密配置/systemd/Nginx/证书/备份脚本归档、release指针、manifest及SHA-256。未配置异地副本。
- 第一次PG隔离恢复由`postgres`直接读取root-only备份目录而失败，空临时库随即删除且未放行发布；改为仅postgres可读的临时副本后恢复通过：107张公共表、head `draft_scene_phase`、1,947份素材响应归档、5个提交。Redis独立端口恢复`DBSIZE=6`且四队列与停机快照一致；项目独立解包1,641文件并包含前端构建产物；私密配置、MCP登记、六服务、Nginx与两套证书清单通过。
- 新旧`uv.lock`、`bun.lock`摘要一致，无依赖变化；无数据库迁移。服务器专用测试库关键9项通过。首次启动检查早于API完成启动且Git归档未包含被忽略的`backend/app/frontend`产物，登录页返回404；确认前端源码无变化后从已备份旧release恢复同一构建产物并只重启API，随后bootstrap、HTTPS、登录页、回调业务错误、API边界、Alembic current/head及四Worker ping全部通过。
- API、资源、结果、搭建、控制、Beat六服务均运行固定SHA且`NRestarts=0`；备份timer已恢复。`MATERIAL_INGEST_ENABLED=true`、`MATERIAL_CLEANUP_ENABLED=true`保持不变，调用策略及上传/共享租约检查通过。

## 真实恢复结果（截至 2026-09-18 17:22 UTC）

- R627 CTA 原证据为`REQUEST_ARMED → NOT_SENT(tiktok_call_deadline_exceeded)`且无请求ID；正式RETRY后产生`RETRY_REQUESTED → REQUEST_ARMED → CREATED`，远端创建阶段约5.3秒，CTA成功65/65。
- 两批原17项`cover_claim_lost`全部退出失败态，没有新增该错误。17:24快照的MATERIAL成功数：R627 `566→708`，6X3F `1134→1310`；其余主要为10组合执行窗口内的PENDING，不是领取竞争终态失败。
- R627 Campaign失败`7→1`，6X3F Campaign失败`12→2`；剩余终态以依赖投影为主，另保留6X3F原有1条`readback_inconclusive`广告，不擅自重建未知对象。
- Redis `builds`在恢复展开后峰值323，约9分钟降至27，下一轮依赖唤醒时回升至67，呈现批量准备后脉冲展开而非单调队列；resources/resource-results/control在17:24快照均为0。服务日志无ERROR、Traceback、OOM或死锁。批次仍在继续推进，本记录不宣称整批完成。

## 效率证据与后续方向

- 17:08起的实际任务：`builds.execute_step` 612次，平均1.089秒、P50 0.261秒、P90 0.601秒、最大27.682秒；`execute_unit` 66次，平均0.230秒。封面准备213次，P50 0.012秒、P90 10.405秒；封面核验29次平均7.594秒。批量封面收口可在35秒内新增30～69项成功。
- 单builds槽接近持续占用；大量0.2～0.3秒任务是本地依赖/窗口推进，少量6～28秒任务等待平台。周期`repair_execution` 12次平均5.357秒，也会占用调度容量。优先优化方向是合并/跳过无状态变化的本地唤醒并降低repair重复扫描，而不是放大TikTok配额或取消安全门槛。
- 主机仅约2GiB内存。观察期Worker当前内存约：资源472MiB、结果255MiB、搭建284MiB、控制121MiB；swap使用约1.2GiB。直接把builds并发从1加到2可能增加约一个Worker子进程的常驻内存并加剧换页，当前未调整。若先扩容至至少4GiB，再用相同批次/配额对比第二builds槽的队列斜率、远端频控和P95，风险更可控。
