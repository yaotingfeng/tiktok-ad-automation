# 2026-10-10 本地发布与单份备份验证

用户确认：本地保留最新一份备份，不上传云存储，完成本地发布流程。目标仍为新加坡 ACK test、tkada.shortclipflix.com，保留三开关 true/true/false，不执行广告或OAuth写入。

## 已完成的工具验证

- 移除 Jenkinsfile 和发布备份的 AWS/age/云桶依赖，新增 deploy/local.py 统一入口。干净副本只包含已提交main，未提交配置和用户改动不会混入。
- 本机备份原子替换 `.runtime/local-release/backups/latest.tar`；新包必须匹配清单、恢复证据和完整回读。失败保留旧包，成功验收后清理本批重复 dump/配置。
- 发布门禁58项（build5、k8s24、release21、本地备份/入口8）。覆盖旧包保留、损坏拒绝、符号链接/同目录保护、未提交文件隔离、固定公司仓库、迁移失败不启动旧版。
- 真正Docker隔离集成通过：PG18恢复、加密授权解密、候选迁移、Redis7→8六类键/TTL、损坏拒绝、本地打包、真实Celery取消消费/排空/恢复后五任务不丢失。
- 本机配置已建立在忽略目录；Bun1.4.2、PG18、kubectl、Docker/buildx就绪。

## 执行中

完整后端/前端门禁正在修复并复测既有问题；线上备份和正式候选发布结果将在执行后补齐。本节不是发布成功声明。

历史集群备份云盘已撤除；ACR中4个旧备份工具tag因缺cr:DeleteRepositoryTag权限仍留存，不运行，也不影响本地备份。

## 真实线上备份完成

- 批次 `20261010T202414-b32e32`，工具提交 `cf901bd`。原运行版本 `673eecb9f928261541be57ecc93b3c7f4350682f` 未切换。
- 本机归档 `.runtime/local-release/backups/latest.tar`，165,416,960字节，SHA256 `7ea6abf2f7c67d9e64913de96618be69c4f6b7d0a86f493276a57425d3c46c84`。
- PG恢复事实一致、4份授权可解密；Redis8个有效键内容/绝对TTL一致（均允许Redis7→8重新编码）。清单摘要绑定、归档回读通过。
- 九Deployment 1/1；七Worker就绪、唯一Beat；镜像index摘要与原版相同，实际配置摘要相同，三个开关true/true/false，HTTPS健康通过。状态complete，锁释放；本批原始dump/配置副本已自动删除，受管目录只有一份latest.tar。
- 两个停服前门禁缺陷已实测修复：ARM本机显式拉取AMD64应用镜像；kubectl连续JSON对象/单List两种输出均正确解析。两次失败均在prepared阶段，原服务未变更，recover已安全解锁。
- 前端最终构建及479项工作台回归通过（无重试/跳过）；后端完整检查仍收尾中，正式应用发布尚未发生。

- 最新归档验收后，按用户仅保留最新一份要求清除ACK旧迁移/演练的38个过期备份载荷及临时验证环境文件，共819,160,356字节；保留清单、审计记录、当前kubeconfig/注册与retired-source清单。清理记录位于私有local-backup-retention-cleanup.json。已结束批次的源码副本也已回收。
- R2真实浏览器验收1/1通过：3文件共16,782,295字节、4次PUT、3完成对象、3次验证投递，未携带平台凭据头。
