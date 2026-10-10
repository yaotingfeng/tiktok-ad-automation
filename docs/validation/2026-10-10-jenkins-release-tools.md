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
