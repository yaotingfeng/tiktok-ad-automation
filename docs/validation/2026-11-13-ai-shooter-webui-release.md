# AI 投手及 DSH WebUI 发布方案

用户明确授权全部本地源代码提交、推送及生产发布。目标为既有 ACK/test 生产集群，域名 `https://tkada.shortclipflix.com`。

- 应用起点为 `a1a78e91`、schema `0128_material_production_origin`；目标增加 `0129_ai_proposal_operation`，operation 历史值为 update，新值仅 create/update。发布语义改变，历史 PENDING 提案标记 STALE，PENDING/RUNNING 复盘取消，要求重新发起，已完成结果保留。
- 使用现有发布器共享锁、配置校验、正常停止受理/排空、精确候选镜像一次性迁移 Job。先正常暂停 DSH，再停止 API/Beat 和消费者；迁移后前向启动候选，禁止自动回退数据库。
- 不新建全备、不执行恢复演练，遵循用户现有发布政策。保留原十一角色、资源、并发、额度、运行 Secret 和四个开关。无 TikTok 广告或素材业务写入验收。
- DSH 配套镜像启用官方 WebUI，9082 仅 Pod 回环；9081 管理代理经平台管理员短会话权限检查。共享 Ingress 原子增加 `/dsh`，保留其他域名/路由；入口 `/api/admin/dsh/entry`。模型凭据仍来自原 Secret，不复制个人会话，模型设置写入持久 profile，重启不得覆盖。
- 原三个搭建修改纳入本次发布：恢复遗漏的已展开执行单元；汇总只计算 INCLUDED 单元。TypeScript 增量缓存/自动生成配置不入 Git。

发布前：管理员 API 与 AI 全模块真实 PostgreSQL 156 项通过；素材默认方案及搭建目录/查询19项通过；工作台端到端 Playwright 15项（含账户/规则弹窗、创建编辑与绑定跳转）及前端构建通过。旧执行 dispatch 五项共同fixture身份目录不可用，未进入主体，不报告为通过。DSH 镜像与线上 WebUI 验收将在实际切换后另行记录。
