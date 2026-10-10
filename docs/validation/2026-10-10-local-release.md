# 2026-10-10 本地发布与单份备份验证

用户确认：本地保留最新一份备份，不上传云存储，完成本地发布流程。目标仍为新加坡 ACK test、tkada.shortclipflix.com，保留三开关 true/true/false，不执行广告或OAuth写入。

## 已完成的工具验证

- 移除 Jenkinsfile 和发布备份的 AWS/age/云桶依赖，新增 deploy/local.py 统一入口。干净副本只包含已提交main，未提交配置和用户改动不会混入。
- 本机备份原子替换 `.runtime/local-release/backups/latest.tar`；新包必须匹配清单、恢复证据和完整回读。失败保留旧包，成功验收后清理本批重复 dump/配置。
- 发布门禁55项（build5、k8s24、release18、本地备份/入口8）。覆盖旧包保留、损坏拒绝、符号链接/同目录保护、未提交文件隔离、固定公司仓库、迁移失败不启动旧版。
- 真正Docker隔离集成通过：PG18恢复、加密授权解密、候选迁移、Redis7→8六类键/TTL、损坏拒绝、本地打包、真实Celery取消消费/排空/恢复后五任务不丢失。
- 本机配置已建立在忽略目录；Bun1.4.2、PG18、kubectl、Docker/buildx就绪。

## 执行中

完整后端/前端门禁正在修复并复测既有问题；线上备份和正式候选发布结果将在执行后补齐。本节不是发布成功声明。

历史集群备份云盘已撤除；ACR中4个旧备份工具tag因缺cr:DeleteRepositoryTag权限仍留存，不运行，也不影响本地备份。
