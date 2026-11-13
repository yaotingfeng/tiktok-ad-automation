# AI 工作台平台管理员灰度生产发布

目标：既有新加坡 ACK / test，入口 https://tkada.shortclipflix.com。用户已明确授权限制管理员、合并分支、推送和发布生产，包含独立 DSH。

## 已选定发布方案

- 线上起点：应用 `74ea9bc0369a8157ec8a30131c9e200bea8dbe6d`，schema `0118_sync_schedule_service_age`，十一角色正常；分支整合当前公司 main 修复和本地 main 的 audience setting 提交。
- 灰度：沿用 `is_superuser`；普通成员和租户管理员没有 AI 导航，直接地址阻断且不请求数据，整个工作台/知识 API 返回403。
- 迁移：审查后选定 `0118→0128_material_production_origin`，新增 AI 业务表/可空来源列，并将旧规则运行唯一约束拆成普通/优化师索引。迁移不清空队列或删除历史业务事实。API/Beat 停止受理，原九 Worker 正常取消消费并排空，十一角色全部零副本后由精确候选镜像一次性 Job 执行，核验 head 后切换同摘要镜像。无旧版自动数据库回退。
- 使用现有发布器的资源归属、共享锁、正常排空和实际配置/镜像验收原语；本批人工编排迁移步骤，不能套用无迁移快修。按现有用户策略不新建全备、不做恢复演练，不伪装成备份已验证。
- 原十一角色并发、资源、额度、四开关、数据库池和 MCP 注册保持。运行 Secret 唯一新增有效 `AI_DSH_SERVICE_TOKEN`，Secret 更新前后逐键核对；不覆盖原有凭据。
- DSH 独立单副本 Recreate，部署名 `tiktok-ad-automation-dsh`，使用独立 app 标签，不改变既有发布器的十一角色清单。复用既有模型 provider/model，凭据通过独立 Secret 注入；20Gi 持久化工作区（云盘最低容量），不复制本机个人会话。仅集群内部健康 Service，Agent 仅开放工作台方法、知识、回测和结果工具。
- DSH 配套 Node 24.22.2、原生 DSH 0.2.2-rc.1 与插件1.3.0，锁定依赖、原生模块构建，启动原生 preset registry。新 profile 必须成功领取 TK-ADA 任务才报告 ready。
- 合并前保存并恢复主目录另三个未提交搭建改动，不将其纳入发布。自动 Jenkins 暂停防止并行切换，完成/中止后恢复原状态。

## 实际生产结果

发布应用及 DSH 镜像版本均为 `a1a78e91ba045194c854a0df49080461abf027fa`。该提交已快进合并 main，并推送 GitHub origin 与公司 company；原三个未提交搭建文件的发布前后补丁逐字节相同，原有未跟踪构建文件保留。

| 项目 | 实际结果 |
| --- | --- |
| 应用镜像摘要 | `sha256:a0e494de9a107b5f51c6d716144e704025737d6c5e7aa51b4201b1ec9ce358fa` |
| DSH 镜像摘要 | `sha256:457016b26a9be68f5a9938c090b9f6661225dbab47494b03dfe418805be8b4c5` |
| 数据库 | 一次性 Job 成功完成 `0118_sync_schedule_service_age → 0128_material_production_origin`，迁移后回查 head 一致 |
| 应用 | 十一角色 Ready、九 Worker 就绪检查、唯一 Beat、HTTPS 通过；镜像摘要、各角色有效配置及数据库池核验通过 |
| 原四开关 | `ADS_SYNC_ENABLED`、`AUTOMATED_RULES_ENABLED`、`MATERIAL_CLEANUP_ENABLED`、`MATERIAL_INGEST_ENABLED` 均沿用线上 `true` |
| 权限 | 通过真实 HTTPS API：平台管理员的优化师/知识接口200，既有普通用户相同接口403；未修改用户角色 |
| DSH | 独立单副本 Ready、0重启；Pod 内 `/ready` 返回200，`connected=true`、插件1.3.0；20Gi状态卷已挂载 |
| 模型 | 选用原有 `com / claude-opus-5-5`，只注入所选模型 Key。Jenkins及实际 DSH Pod 内的无业务连接请求均返回200和预期标记 |
| 公共知识 | 新剧补贴 Q3/Q4、短剧变现链路两个预置包经正式 HTTPS 领域 API 导入 PUBLIC v1，保留来源未核实/适用条件标记 |
| 发布协调 | 发布器已完成、共享锁已释放；Jenkins 原启用状态在收尾时恢复（以下进度记录核对） |
| 备份 | 按用户现行策略未新建全库备份、未做恢复演练；没有报告为已验证 |

本地权限相关真实PG153项、页面24项、TypeScript/Vite/Ruff通过。DSH 在实际 AMD64 镜像、非root、只读根文件系统及无外网测试环境下8项原生测试通过。最初 Docker tmpfs 默认 noexec 导致原生库加载失败，明确使用 exec 临时目录后通过；生产磁盘 emptyDir 下原生启动和领取成功，未放开根文件系统写权限。

切换前在途目录发布任务正常排空。切换后历史回补 Worker 因资源碎片和原有反亲和约束 Pending；仅对本次新启动的素材、目录、报表 Worker 正常缩容退出，先恢复回补、再恢复报表与其余 Worker，解决落点冲突。最终十一角色仍一副本，Deployment 模板、资源、并发和反亲和配置不变；未操作其他项目资源及既有身份维护 Pod。

首次在 Jenkins 原生进程执行预检时，宿主 Docker 挂载路径与 Jenkins 容器路径不一致，预检在停服前阻断；随后改用既有 CI 工具容器及宿主同路径挂载继续本批共享锁，没有绕过预检。迁移成功后只向前恢复候选版本，没有启动旧程序或回退数据库。

尚未创建真实优化师、广告、素材订单或进行真实经营复盘；本轮模型证明是连接请求，原生方法/任务工具证明是隔离容器测试，二者不冒充真实业务闭环。成熟窗口与旧历史事实缺失仍按实际覆盖降级。外部素材生产方的 `production_origin` 回传尚需其配套实现；缺谱系时不宣称素材制作策略归因可用。
