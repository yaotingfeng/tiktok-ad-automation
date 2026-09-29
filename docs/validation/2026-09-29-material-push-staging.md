# 2026-09-29 外部素材批量推送：测试服发布

## 版本与授权

- 新加坡测试服务器 `137.220.150.31:22211`，HTTPS 入口 `https://tk-ada.137-220-150-31.sslip.io`；不是生产服务器。
- 用户明确要求推送、部署、配置可调用接口，并提供含凭证的私有文档；随后明确取消来源域名白名单和租户配置名单，允许所有有效租户。
- 代码 `04087905dee3cce83ff04ba1804455927a0e5066` 已推送 `origin/main` 并部署；包含前置原名保留 `3b841cb` 和批量推送 `098cdd5`。原运行代码 `6ef4661e58cf99d212ce051f5c3b37324fd8ea14` 保留。
- 数据库由 `audience_targeting` 升级为唯一 head `material_push`，新增四张推送/来源表、租户名称唯一约束及默认 BC；迁移前无重名租户。未执行 downgrade 或改写历史数据。

## 配置变化

| 项目 | 发布前 → 发布后 |
| --- | --- |
| `MATERIAL_INGEST_ENABLED` | true → true；用户要求启用推送，扩展范围包含新接收/校验任务 |
| `MATERIAL_CLEANUP_ENABLED` | true → true；外部原件不进入删除链路 |
| `MATERIAL_PUSH_CLIENTS` | 无接入方 → 一个已验签的系统接入方；仅 secret/actor_id，无租户或域名名单 |
| 执行身份 | 新增独立非平台管理员用户 `material-push-service`；随机登录密码仅用于哈希，不交付浏览器登录凭证 |
| 租户成员 | 首次推送时按需建立 operator；已有停用/只读成员不覆盖 |
| 既有配置 | 加密密钥、DB/Redis、R2、调用额度、媒体预览主机、MCP 和调度配置保持一致 |

全部有效租户含后续新增租户均可按准确名称接入，不自动创建不存在的租户。公网 HTTPS、DNS/IP 防内网访问、固定连接 IP、TLS、无跳转及文件限制保持。接口仍需 HMAC 签名，不是匿名公开上传。

私有配置服务器文件 root:tt-ada 0640；受控本地 `.env.staging` 已同步，原本地副本保存在忽略目录。真实 key/secret 只在受控配置、服务器 root 私有目录及本地 `.runtime/singapore-staging/素材工具AI-Agent接入文档-测试环境-私密.md` 中，文件 0600，不提交 Git。公开接口文档见 [material-push.md](../integrations/material-push.md)。

## 完整备份与恢复

批次 `/var/backups/tt-ada-staging/20260929T070243Z/`：正常停止 API/Beat、排空四个 Worker 并停止后备份，暂停日常备份 timer；结束已恢复 timer。

- `postgres.dump`、`redis.rdb`、`config.tar.gz`、独立 `project.tar.gz`、`runtime-config.tar.gz` 的 SHA-256 全部通过。
- 项目独立解压比对 1,347 个文件，包括源码/迁移/锁文件/实际前端构建；可重建依赖与缓存排除并记录。旧 release 不是唯一项目备份。
- 私有配置含环境/加密密钥、MCP 注册、全部 systemd/Nginx 站点、证书及备份脚本；独立解压关键文件逐字相同。
- Redis 在独立 Unix socket 实例加载，读取 5 个键成功，未覆盖业务 AOF；校验实例正常退出。
- PostgreSQL 恢复至独立 `_test` 数据库；22 张历史业务表摘要一致，10,484 份加密响应解密/长度/SHA-256 一致，既有连接凭据可解密。历史摘要仅排除新增 `default_bc_id` 和原脚本已有排除列。
- 恢复库先演练升级及 `alembic check`，再升级业务库；前后历史验证通过。批次含版本/head/工具链/大小/排除项/恢复位置和 `RELEASE_COMPLETE`。
- 备份及旧版本保留，未自动清理；未配置异地副本，同机备份不是主机损坏保护。

## 实际服务器验证

- 六服务运行，API/全部 Worker/唯一 Beat 共 11 个进程的实际工作目录为新 release；按各进程环境重建 Settings，全部含新凭证且配置摘要一致。排除新增接入项后，发布前后原配置摘要相同；入库/清理仍 true，调用额度和租约校验通过。
- 四 Worker 回应 ping，均注册 `materials.import_external` 和 `materials.repair_external_imports`；FFprobe 可用。备份和证书续期 timer 都 active。
- HTTPS 健康、登录 HTML/引用静态资产字节、平台与租户管理员登录及权限隔离通过。
- 实际 key 的 POST 空字段返回 422（证明验签通过后进入参数校验）、签名 GET 不存在批次返回 404；无签名或篡改 body 返回 401。开发机从外网 HTTPS 独立复核同样通过。
- 两个实际租户 `junbo`、`xingyu` 的签名请求均正确解析，再因 `127.0.0.1` 来源被拒绝为 `push_url_invalid`，事务回滚，无测试批次残留。
- 使用本地业务目录检查两个租户的默认 BC/连接/主素材账户上传权限通过，检查事务回滚；没有触发平台同步、授权或上传调用。
- 推送批次数仍为 0。真实素材从读取到 TikTok `available` / `video_id` / 原文件名回读尚未验收；不能把接口验签可达、路由可用或 202 当成真实上传成功。

## 本地测试与已知范围外失败

- 新的“任意公网来源受理”“新租户无需配置成员”用例先在旧实现因缺少名单配置而失败；修改后推送/迁移/租户 88 项通过，最终扩展旧读取及 URL SDK 共 201 项通过（重叠批次不累加）。
- Ruff、`ty check app`、TypeScript/Vite build、差异检查通过。
- 全仓 `pytest -q --maxfail=5` 在 143.38 秒后止于以下五项，并非全仓通过：
  - `tests/acceptance/test_batch_flow.py::test_real_preparation_freeze_submission_and_official_sdk[jiashu]`
  - `tests/acceptance/test_batch_flow.py::test_real_preparation_freeze_submission_and_official_sdk[wangyan]`
  - `tests/acceptance/test_batch_flow.py::test_preparation_and_freeze_are_real_and_have_no_tiktok_writes`
  - `tests/acceptance/test_batch_flow.py::test_partial_input_and_material_matching_use_actual_preparation[currency]`
  - `tests/acceptance/test_batch_flow.py::test_partial_input_and_material_matching_use_actual_preparation[minis]`
- 首个失败为未修改的 `local_read_batch.py` 用包含 dict 的 tuple 作缓存键。将修改前 `098cdd5` 导出至独立 `/tmp` 快照，显式 PYTHONPATH 指向快照 backend，29.36 秒复现首项同一异常；没有把此广告搭建问题混入素材接入改动。其他四项未逐一做基线复现。
- 误执行不带范围的 `ty check` 会覆盖非正式检查范围 scripts/tests 并给出 5265 项诊断；仓库脚本规定的 `ty check app` 无错误。前一轮另有两项既有素材测试失败，见 [本地记录](2026-09-29-material-push-local.md)。
