# 2026-10-09 公司测试部署就绪验证

## 结论与范围

公司 K8s 部署适配及本地容器验收通过，可进入目标环境资料确认、镜像发布和集群测试部署阶段。本轮没有推送镜像、连接/写入公司集群、改动现有测试服务器或执行真实广告操作；不等于集群或生产验收通过。

基线为 `e223be8`。数据库选型和数据库业务改造不在本轮范围内；本地启动验证使用本轮新建的隔离临时依赖。用户原有未提交 `AGENTS.md` 改动保留，不纳入本轮提交。

## 完成的适配

- 统一 Dockerfile/API/K8s Service 的 80 端口；同步 Compose 命令、健康检查、端口映射和代理示例。
- 构建前要求 `main`、干净工作区、真实 HEAD、公司 Gitea `main` 精确匹配；固定本项目 TCR 地址并写入镜像 revision 标签。部署拒绝 `latest`、其他项目和 SHA 不匹配的镜像，并用本地镜像标签再次核验。
- 部署前在无网络容器中运行应用实际配置校验器，验证注册文件、加密密钥、对象存储配置、额度结构及上传租约；失败不访问集群。
- 所有集群命令必须显式指定 context 和 namespace。覆盖前查重、核对已有资源归属/镜像；两个 Secret 只按本项目精确名称读取并写入项目标签。跨 namespace 同名/同标签和其他入口域名冲突均阻断。
- Ingress 追加使用 resourceVersion 乐观锁，核对项目 Service 后仅追加自己的规则，完整回读已有规则与新增规则。
- 移除测试 IP/sslip.io 域名及隐式启用开关；目标域名、前端 origin、回调必须一致，三个部署开关显式填写。
- 九个应用角色配置资源请求和上限，Worker 初始并发为 1，增加七个 Worker 就绪探针；Beat 单副本 `Recreate`。
- Job 名称控制在 Kubernetes 长度限制内，完整 SHA 和配置摘要写注解；配置变化触发应用重新加载。
- MCP Secret 改为单文件 subPath 挂载，兼容注册读取的 `O_NOFOLLOW`；避免普通 Secret 目录挂载中的符号链接导致授权配置不可读。
- 部署入口、只读预检、环境隔离、首次开关取值、备份、验收与回退步骤统一记录在 `deploy/k8s/README.md`。

## 验证证据

| 检查 | 结果 |
| --- | --- |
| `node scripts/test-deployment-assets.mjs` | 2 个入口套件，底层 4 项构建 + 17 项部署行为回归，共 21 项通过 |
| `uv run --with kubernetes-validate python scripts/validate-k8s-manifests.py` | 13 个对象通过 Kubernetes 1.34 严格 schema 与端口、挂载、资源、Beat 更新策略校验 |
| `bash -n` / `node --check` / Python compileall / Ruff / `git diff --check` | 本轮修改的脚本通过 |
| Compose local/staging/production 三个组合 | 使用合成环境渲染通过；后端命令、健康检查、容器目标端口均为 80 |
| Linux ARM64 镜像 | 真实多阶段构建通过，包含前端 TypeScript/Vite 构建及冻结后端依赖安装 |
| Linux AMD64 镜像 | 真实目标架构构建通过；后续完整容器验证使用该镜像 |
| 实际运行配置校验 | 合成有效配置通过；空额度配置被拒绝 |
| 九应用角色启动 | 直接使用 K8s 清单里的命令与 CPU/内存 limits；全部运行，未发生 OOM |
| Web/API | 80 端口健康检查、内置前端、管理员登录/token、受保护用户接口、未知 API 404 均通过 |
| 七个 Worker | 实际 prefork Worker 的就绪探针逐一通过 |
| Beat / API 恢复 | 单个 Beat 运行；API 重启后健康检查恢复 |
| 清理 | 本轮 Docker 容器、临时匿名卷与隔离网络已移除；未使用 prune |

构建产物为本地验证镜像，revision 为 `local`，不伪装为已推送的正式 SHA 镜像：

- ARM64：`tiktok-ad-automation:deploy-check`，本地 image ID `sha256:1c3798ace8c04237acfe6a2cdba4937a1401518483ac083698e352c9919a39c5`。
- AMD64：`tiktok-ad-automation:deploy-check-amd64`，本地 image ID `sha256:0c688bb71dadfd965e9e4f82b253b54851fd009c0093af067db9782425270a85`。
- 本机日志：`.runtime/deployment-readiness-20261009/build-arm64.log`、`build-amd64.log`、`local-smoke-amd64.log`（忽略于 Git）。
- kubectl 1.34.1 客户端已下载并校验官方 SHA-256，位于 `.tools/bin/kubectl`；未安装/替换 kubeconfig，也未连接集群。

启动稳定后的单次内存快照：API 约 189 MiB，Worker 各约 221–258 MiB，Beat 约 202 MiB。这只是空载启动值，不代表素材处理峰值或生产容量结论。

## 实际部署时仍需完成

1. 明确目标 context、运维分配的域名/DNS、现有通配证书及集群版本/配额；Kubernetes 1.34 离线 schema 校验不能代替目标集群服务端检查。
2. 确定独立测试或接替旧测试实例、隔离队列/业务状态与旧调度的处理方式；按既有发布手册完成应有备份及排空。
3. 填写该环境真实私有配置，确认三个开关取值；完成目标域名对应的 API/MCP 注册和 R2 CORS。此处不复用合成验收凭据。
4. 按授权推送本轮提交至公司 Gitea，构建/推送对应 SHA 镜像，运行只读预检，再执行集群部署。
5. 验证真实 DNS、HTTPS、Pod rollout、Worker、配置一致性及外部业务能力。真实广告写入须有具体授权。

此前全仓历史回归仍有待复核项，见 `2026-10-09-regression-followups.md`。本轮验证覆盖部署适配与容器启动，不将其称为全仓业务回归通过。
