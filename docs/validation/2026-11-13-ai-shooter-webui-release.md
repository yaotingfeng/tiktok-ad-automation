# AI 投手及 DSH WebUI 发布方案

用户明确授权全部本地源代码提交、推送及生产发布。目标为既有 ACK/test 生产集群，域名 `https://tkada.shortclipflix.com`。

- 应用起点为 `a1a78e91`、schema `0128_material_production_origin`；目标增加 `0129_ai_proposal_operation`，operation 历史值为 update，新值仅 create/update。发布语义改变，历史 PENDING 提案标记 STALE，PENDING/RUNNING 复盘取消，要求重新发起，已完成结果保留。
- 使用现有发布器共享锁、配置校验、正常停止受理/排空、精确候选镜像一次性迁移 Job。先正常暂停 DSH，再停止 API/Beat 和消费者；迁移后前向启动候选，禁止自动回退数据库。
- 不新建全备、不执行恢复演练，遵循用户现有发布政策。保留原十一角色、资源、并发、额度、运行 Secret 和四个开关。无 TikTok 广告或素材业务写入验收。
- DSH 配套镜像启用官方 WebUI，9082 仅 Pod 回环；9081 管理代理经平台管理员短会话权限检查。共享 Ingress 原子增加 `/dsh`，保留其他域名/路由；入口 `/api/admin/dsh/entry`。模型凭据仍来自原 Secret，不复制个人会话，模型设置写入持久 profile，重启不得覆盖。
- 原三个搭建修改纳入本次发布：恢复遗漏的已展开执行单元；汇总只计算 INCLUDED 单元。TypeScript 增量缓存/自动生成配置不入 Git。

发布前：管理员 API 与 AI 全模块真实 PostgreSQL 156 项通过；素材默认方案及搭建目录/查询19项通过；工作台端到端 Playwright 15项（含账户/规则弹窗、创建编辑与绑定跳转）、工作台合同/权限12项及前端构建通过；独立 PostgreSQL 库从空库执行完整 Alembic 升级至0129成功，operation约束已核对。旧执行 dispatch 五项共同fixture身份目录不可用，未进入主体，不报告为通过。DSH 镜像与线上 WebUI 验收将在实际切换后另行记录。


## 已完成生产发布

- TK-ADA 应用镜像 `00f7d1cd0986065cb880d469111f41b454542646`；DSH 独立镜像 `3ca20a3f6017730fc8a6d04301137abc51ece91e`。后两次修复只涉及 DSH 启动/挂载，不重启已验收的 TK-ADA。代码均已推送 GitHub 与公司 main。
- 0128→0129 一次性迁移成功；旧消费者正常排空，队列保留。旧待确认提案 STALE、未完成复盘取消，需要按新发布合同重新复盘；已完成历史保留。
- 十一应用角色全部 Ready/0重启、九 Worker 与唯一 Beat 已恢复，精确镜像摘要、实际运行配置/数据库池、四开关及 HTTPS 核对通过。四开关保持 true。报表启动出现资源碎片，正常迁移本项目素材 Worker 后恢复全部原副本；没有调整资源、亲和性或并发。启动验收在迁移素材 Worker 的过程中暂未通过，待原副本就绪后使用既有状态完成验收，未重复数据库迁移。
- DSH 官方 WebUI 经平台管理员短会话访问：未登录401、普通用户换会话403、管理员200；四项JS/CSS资源200，实际 `/dsh/api/remote.mux` WebSocket101。浏览器已实际打开官方页面、默认工作区和“设置→模型”。
- 默认工作区位于 `/var/lib/dsh/workspace/deepseek-harness/default-workspace`，位于原20Gi持久卷；实际初始化RPC通过。默认模型仍为 `com / claude-opus-5-5`，原模型Secret未重写。凭据文件为0600，最终DSH Pod Ready/0重启。
- 两项真实部署缺陷已修正并验证：Linux容器无Documents导致默认工作区创建失败；Kubernetes fsGroup在重挂载时放宽凭据文件权限导致官方插件拒绝启动。原生测试覆盖持久工作区初始化、凭据内容与权限保全，最终AMD64镜像12项全部通过。
- 验证范围：后端156+19、浏览器15+12、部署合同25、DSH镜像12项通过及前端构建、完整迁移链。旧dispatch五项fixture身份目录故障未进入主体，未称为通过。没有真实广告创建、素材下单或经营复盘验收；没有新备份或恢复演练。

管理员入口：[DSH管理台](https://tkada.shortclipflix.com/api/admin/dsh/entry)。请先登录TK-ADA平台管理员；工作台侧栏亦提供入口。“账号菜单→设置→模型”管理模型配置。

精确版本与只含非敏感字段的线上证据见 [发布回执](2026-11-13-ai-shooter-webui-production.json)。
