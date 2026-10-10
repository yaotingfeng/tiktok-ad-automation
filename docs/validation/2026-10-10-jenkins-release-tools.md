# 2026-10-10 Jenkins 发布工具验证

## 完成范围

新增根目录 Jenkinsfile（main 自动测试/构建/发布，其余分支测试）、Jenkinsfile.backup（独立一致性备份 Job）、deploy/ci/check.sh、deploy/release 发布状态机/备份/恢复/说明。复用现有 apply，增加迁移后九角色零副本阶段；构建允许 Jenkins detached checkout，仍要求完整 SHA 等于公司 main。

集群独占锁保留到验收完成；旧版恢复核对 context/UID/模板/Secret。取消七 Worker 的消费，连续两次检查在途/预取/延迟任务与订阅为空，停 Worker 后核对 Redis unacked；队列中的业务消息不要求清空。备份包括 PG public schema、DB15、Beat、源码/运行前端和配置，隔离恢复通过后 age 加密上传，下载核对摘要才允许正式迁移。迁移失败不自动回滚。

运行配置从线上本项目 Secret 读取；普通发布拒绝静默修改 ConfigMap 默认值。验收检查九角色版本与运行镜像 digest、运行配置/注册文件摘要、开关和 HTTPS。RELEASE_HOLD_WORKLOADS 仅为部署脚本内部编排参数，不注入应用，不新增业务开关。

## 本地结果

- `node scripts/test-deployment-assets.mjs`：5 项构建、24 项既有 K8s、17 项新增发布门禁，合计 46 项通过。
- `python3 scripts/test-release-integration.py`：真实隔离 PostgreSQL18/Redis8、真实 app Celery 配置和 prefork Worker，纯合成数据。PG 真实迁移建库、备份恢复、加密凭据解密及候选 migration head 验证通过；Redis string/list/hash/set/zset/带 TTL 键恢复通过；损坏归档拒绝；取消消费后积压保留，恢复消费后五任务全部完成。
- Ruff、Python 编译、shell 语法、CLI help、git diff --check 通过。
- 外部 kubectl/AWS 运输边界使用替身验证错误分支；真实 PG/Redis/Celery 不使用数据层替身。集成容器、网络与临时 tag 已按本轮 ID 清理。

未执行：公司 Jenkins Declarative 在线校验、Agent 全量 CI、Gitea webhook、真实私有桶上传/解密演练、真实生产发布/恢复。未读取或修改线上业务数据、广告、授权；不把本地测试报告为生产验收。已有 Gitea 历史 CI 失败未因此宣称修复，流水线遇到失败将停止。

## 接入待办

公司提供 Jenkins 地址/Job 配置权限、Agent/模板，以及专用私有桶和凭据托管方式。凭据 ID、网络/依赖和 RBAC 见 deploy/release/README.md。配置私钥保管、备份保留策略、备份时段与失败通知后，执行首次实际 Jenkins 验证。BACKUP_CRON 默认空，避免未约定窗口就定时停服；托管 PG PITR/Redis 云备份仍须运维单独核实。

## 后续清理与全流程复核（用户要求，2026-10-10）

结论：已有分阶段脚本与部分实际运行证据，但尚未具备供非技术用户直接使用的端到端一键发布。此前“已写好一键发布”只能解释为编排代码已实现，不能作为已接通、已验收的交付结论。

| 环节 | 本轮核对结果 |
| --- | --- |
| 提交/推送 | main可正常提交并推公司Gitea；不会自动提交全部工作区。用户/并行任务未提交改动保留，正式构建需干净副本与精确公司main SHA。 |
| 测试 | 发布门禁48项通过；从公司main 17f5961的干净Git归档检查后端，Ruff 0.16.4发现6项问题，ty 0.0.74使用项目虚拟环境发现120项诊断，版本与uv.lock相同。完整CI尚未通过，不跳过这些失败；不是线上120个已证实故障。 |
| 构建/上传 | build-and-push.sh支持AMD64镜像按完整SHA上传新加坡ACR，此前673eecb实际完成；本轮没有构建/上传新应用镜像。不能把旧版本成功视为当前main完整通过。 |
| 发布前备份 | release.py保存本机/Agent私有目录，恢复演练后age加密上传私有S3兼容存储并回读校验。BACKUP_BUCKET/凭据尚未配置，远端备份链路未验收；阿里云各实例定时备份不能替代数据库/队列/配置同一窗口的发布恢复包。 |
| 发布/验收 | 已实现暂停、排空、迁移、九角色恢复与验收。此次修复仓库index digest与ACK imageID不一致的门禁错误；新增两项回归并实测9个运行角色全部匹配。没有执行线上新版本发布。 |
| 故障恢复 | 有recover/finish及状态锁；发生异常仍可能需要人工判断。迁移后不自动回退数据库，无法承诺无人值守自动修复。 |
| 自动触发 | Jenkinsfile存在，但公司Job/Agent、凭据、webhook和失败告警尚未接通，未完成首次真实流水线。 |

复现检查采用独立Git归档，未混入用户未提交的AGENTS.md及accounts/tasks.py；保留检查日志在私有 `.runtime/release-review-20261010/`。Ruff/ty失败属于既有业务代码范围，本轮未扩大成全业务代码修复，也未修改门禁放行。

已移除独立每日停服备份：ACK CronJob、专用ServiceAccount/Role/RoleBinding/NetworkPolicy/ConfigMap/StorageClass、备份PVC/PV、已结束临时任务及维修Pod。将核对过UID的备份卷改为Delete回收策略后交CSI释放，仅针对云盘 `d-t4nevxnr09owj49rrvqk`；云控制台新加坡完整17盘列表已确认该ID不存在。九服务UID/spec/1副本就绪状态保持不变，原Beat云盘保留。

删除deploy/backup-cluster、配套测试及Jenkinsfile.backup，保留发布前备份模块。试验构建上下文、一次性脚本和重复归档已清理，本机5个备份工具镜像tag已删除。旧迁移备份保留，真实冻结试验归档经manifest校验后移至私有 `ack/backup-experiment-evidence-20261010/`；该试验归档的Redis验证曾失败，不能标作完整验收通过。

到可用的一键发布还须按顺序完成：修复完整CI失败；确定并接通发布前备份存储及解密回读演练；配置可信发布执行环境/触发方式/权限和失败通知；对明确候选SHA执行一次完整发布与故障恢复验收。仅增加包装命令不会消除这些缺口。


远端工具镜像清理受权限阻断：本项目ACR仓库4个 `backup-` tag（07035bb、b7fad56、6cb1779、5d313cd）仍保留。控制台尝试删除5d313cd时返回 `AUTHENTICATION_FAILED`，明确缺少 `cr:DeleteRepositoryTag`（资源组级身份策略）。未绕过权限或改删仓库，正式673eecb镜像保留；管理员可只删除上述4个工具tag，不得删除正在运行的应用tag。
