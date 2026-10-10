# Jenkins 发布与备份实施计划

**Goal:** 为现有 ACK test 生产实例提供可恢复的一键发布、独立备份及 Jenkins 入口。

**Architecture:** 复用现有镜像和 Kubernetes 部署门禁。先取消七个 Worker 的消费，待已领取步骤结束后停服务；队列不要求为空。私有备份经隔离恢复、加密上传与下载校验后才迁移；发布失败保留状态和集群锁，不自动回滚数据库。

**Tech Stack:** Python 标准库、kubectl、Docker、PostgreSQL 18、Redis 8、age、AWS CLI、Jenkins。

**Spec:** 用户已确认 main 自动测试/构建/发布、其他分支只测试；遵循 production-junbo.md、deployment.md。

## 约束与重点验证

- 固定 ACK context、test、本项目资源；保留用户 AGENTS.md 改动。
- 集群锁禁止发布和备份重叠，不自动抢过期锁；恢复必须匹配上下文、资源 UID、旧镜像与配置。
- 取消消费必须收到全部 Worker 回执；失联、在执行/预取/延迟任务不允许强杀进入备份。
- 备份完整包括 PostgreSQL、Redis DB 15、Beat、运行代码/前端、配置及本项目部署声明；共享证书使用 ALB 引用，不读取他人私钥。
- 数据库恢复验证使用隔离容器与真实 PostgreSQL/Redis，无法验证则拒绝发布。
- 无 Jenkins/私有桶凭据时完成脚本、测试与说明，实际接通状态如实记录。

## 步骤

- [x] 发布状态机和独占锁、暂停/恢复、失败保留现场的行为测试。
- [x] 备份、校验、隔离恢复、加密远端上传和回读；真实隔离数据测试。
- [x] 复用 apply 的零副本发布方式、发布后配置和 HTTPS/Worker 验收。
- [x] Jenkins 测试/构建/发布与独立备份入口、凭据说明。
- [x] 执行回归、更新进度、聚焦提交并推送公司 main。

仓库实现与本地验证完成；Jenkins 服务接通、私有桶上传和真实生产发布仍待外部资料，未以本地测试代替。验证记录：`docs/validation/2026-10-10-jenkins-release-tools.md`。
