# ACK 集群备份

本目录将 release 的一致性备份运行在 ACK `test`，无需 Jenkins 或个人电脑。当前仅保存到项目独立 20 GiB PVC；独立 StorageClass 使用 `Retain`，误删 PVC 不自动删除底层云盘。云盘同区域副本不等于异地灾备，托管 PG/Redis 备份和私有 OSS 仍需云权限。

## 组成与权限

- CronJob `tiktok-ad-automation-backup`，北京时间 03:00、并发 Forbid、失败不自动重试。首次 render 为 suspend，完成实际验证后启用。
- 控制器使用独立 ServiceAccount；仅九 Deployment 的 scale 写权限，三个精确项目 Secret 读取，项目状态锁及临时 Pod 所需操作。没有数据库迁移 Job 权限、没有共享 Ingress 写权限、没有 Secret 列举权限；旧 prod 仅九 Deployment get。
- Kubernetes 原生 RBAC 无法按标签限制 Pod create/exec。因此该账号仍有 test 内 Pod 创建/执行权限，必须只给此受信任工具，不用于通用 CI/不可信分支；Pod 创建间接具有挂载 namespace Secret 的能力。后续若公司要求更强隔离，应加 admission 策略或独立 namespace 架构，不能把此权限称为严格的项目级隔离。
- 控制器靠近 Beat 调度，复用其云盘所在节点；不修改九服务镜像或运行配置。恢复 Pod 独立 PG18/Redis8，仅监听 localhost、无 ServiceAccount token，NetworkPolicy 拒绝外部网络，停服前实测不能连接正式 PG/Redis/API。

## 备份与恢复验证

复用 deploy/release/release.py 的独占锁、停止 API/Beat、取消七 Worker 消费、等待在途步骤结束和恢复服务。业务队列不清空；暂停超时就阻断。纯备份失败优先核验后恢复旧服务，Job 仍失败；恢复失败保留状态/锁，不盲目强杀。

冻结后归档 PostgreSQL public schema、Redis DB15 的 DUMP/绝对 TTL、Beat、当前镜像 `/app` 的程序与前端构建、本项目配置/Secret和部署声明。`project.tar.gz` 在集群模式为运行程序归档，与 runtime-app 内容一致；完整 Git 源码仍在公司 Gitea，不把这份归档误称为包含未打进镜像的前端原始源码。每次恢复验证比对业务表行数、迁移 head、历史凭据可解密、Redis有效键内容/TTL。无广告消费者或外部业务调用。

age 公钥保存在项目 ConfigMap，私钥只保存在操作者本机私有目录，禁止上传集群或提交 Git。归档加密后重新读取校验 SHA256；九服务恢复/验收通过后删除明文、发布 latest.json。保留最新 14 个通过验证的加密批次，只有新批次完整成功才清理旧批次；失败现场不自动删除，已有密文校验失败时停止清理。保留数量不是时间点恢复能力。

`/backups` 是独立云盘目录。`completed.json`、`encrypted.json`、`restore-verified.json`、`acceptance.json` 均无密钥；失败批次可能包含明文配置和备份，目录0700/文件0600，须由管理员处理。Kubernetes Job状态/失败日志可由现有集群监控采集，尚未接通消息告警；不能把 Job Failed 当成已经通知负责人。

## 手动执行/故障恢复

```bash
kubectl --context kubernetes-admin-c19dbd82590a34b9f87cebb8c1a858796 -n test \
  create job --from=cronjob/tiktok-ad-automation-backup tiktok-ad-automation-backup-manual-<唯一批次>
```

维护时先 suspend CronJob，等待已开始任务结束；与 Jenkins 发版还通过同一个 release-lock 互斥。不要删除正在执行的备份 Pod，它可能处于停写窗口。

故障时先检查九 Deployment、release-lock 和私有 state.json，再使用同镜像/账号/云盘的维修 Pod 执行 controller.py recover --state-dir /backups/<批次>。若 phase=resuming，则使用 finish。维修 Pod须 nodeName 指向云盘当前挂载节点；不要把需要操作的生产数据恢复到原库。完整说明继承 deploy/release/README.md。

下载 `backup.tar.age` 到私有目录并核对 encrypted.json 的摘要，用离线 age 私钥解密，按 manifest 核对所有文件。只在隔离 PG/Redis 实例恢复验证；禁止覆盖生产库或共享 Redis。应额外保存一份离线私钥和独立地点备份，否则集群云盘损坏或私钥丢失仍可能不可恢复。

## 发布工具本身

Dockerfile 基于已验证项目应用镜像，增加 Debian age 和官方 SHA256 校验的 Linux amd64 kubectl 1.36.2。构建上下文仅代码与公开二进制，不含 .runtime 或真实凭据。工具镜像使用同一项目仓库 `backup-<完整GitSHA>` tag，CronJob 固定 digest；不能替换九应用角色的普通 SHA tag。

`render.py <VPC镜像@sha256:...>` 输出 StorageClass/PVC、RBAC、隔离策略和暂停 CronJob。首次创建检查名称/归属后 create；更新须检查既有 UID/归属，不能无条件 apply 他人资源。公钥和 retired-source 清单单独从私有交接资料生成 ConfigMap。

验证：`node scripts/test-deployment-assets.mjs`、`python3 scripts/test-release-integration.py`。真实集群验证证据另记到 docs/validation，不用合成测试代替生产恢复验收。
