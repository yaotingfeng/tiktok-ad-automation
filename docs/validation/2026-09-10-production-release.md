# 2026-09-10 TT ADA 骏伯生产发布验收

## 发布结果与版本

2026-09-10（北京时间）已在用户指定主机完成首次生产发布和基础验收。入口 **https://manjuad.gzjunbo.net:8000/**，同端口 HTTP 返回 308 转 HTTPS。8000 已验证可用，无须另换端口；原域名 80/443 项目保留。

- 应用 Git SHA：`016217f65a39330b4b715ab043fe866eea87810c`，已推送 `origin/feat/platform-implementation`（仓库当前默认分支，无 main）。
- 镜像：`tt-ada:016217f65a39330b4b715ab043fe866eea87810c`，镜像 ID `sha256:529ae07b3f3f7bc6a228c6795186e7d3dff063c4b7e35623aeba81ceba6f58a5`。API、三个 Worker、Beat 实际使用同一镜像。
- `/opt/tt-ada/current` 指向上述完整 SHA 的版本目录；Compose 项目 `tt-ada-production`。
- 首次空库通过全部 Alembic 迁移，`current` 与唯一 `head` 均为 `r2_part_receipts`。生产数据库独立初始化，没有复制开发库。
- 运行服务：PostgreSQL 18、Redis 8、API、resources/builds/control 三个 Linux prefork Worker、一个 Beat。API 仅绑定 `127.0.0.1:18000`，TT ADA 数据库和 Redis 不发布宿主机端口。
- 平台管理员 `admin`；租户 `junbo`，租户管理员 `junbo`。两者为不同用户记录，密码按用户指令设置，凭据不写入仓库。
- 验收及规则完善的后续文档提交不改变此运行镜像；下次发布必须核对实际 current，不能以仓库最新文档提交推断生产代码已更新。

## 实际验证

| 范围 | 结果 |
| --- | --- |
| 生产构建 | 本机 Docker 实际完成前端生产构建、冻结依赖安装、后端镜像构建；不以历史 CI 代替本次证据 |
| 入口 | 公网 HTTPS 证书正常，无忽略证书校验；HTTP 308 保留路径转同端口 HTTPS |
| Bootstrap | `scripts/check-bootstrap.py https://manjuad.gzjunbo.net:8000` 通过：健康、静态登录、回调业务错误及未知 API 404 |
| 平台账号 | admin 实际 API/浏览器登录、平台租户和用户列表通过 |
| 租户账号 | junbo 实际登录，只属于 junbo 租户且为 tenant_admin；平台用户、平台租户及无权限租户接口返回 403 |
| 页面 | 平台用户/租户、租户账户/授权连接/成员/版权方/素材/搭建/任务/策略页面通过真实浏览器检查；浏览器安全上下文与 crypto 能力正常 |
| 策略 API | 创建、相同请求幂等回放、追加 v2、回读与停用通过；全局文案池 100 条 |
| 策略 UI | 使用正常点击创建（含预算/创意配置）、修改预算、保存新版本、刷新读回、停用均通过；完整浏览器运行 19 项检查通过，无 JS 异常、5xx 或被保护规则拦截的操作 |
| 后台任务 | 三个 Worker ping 通过且各自队列正确；真实 PostgreSQL outbox → Beat → control Worker 的 jobs.probe 被发布一次并执行成功 |
| 重启 | API 正常重启后，两类账号登录和页面只读复验 18 项通过，bootstrap 再次通过；账号和业务记录保留 |
| 服务观察 | 7 个 TT ADA 容器运行正常、自动重启计数为 0；db/redis/backend 健康，检查的近期应用日志无 ERROR/Traceback/CRITICAL |
| 共存 | 原站 80/443 均为 200，HTML SHA256 与部署前一致；原有 8 个应用容器仍健康，未操作其数据与配置 |

首轮 UI 验收在创建后立即编辑时，被鼠标悬停中的成功通知遮挡保存按钮，尚未完成该步。复验移开鼠标并等待通知正常消失，然后正常点击完成全流程；没有 force click、删除 DOM 或 mock API。保留首轮失败记录，不将其表述为全通过。验收共留下 3 条明确命名“上线验收”的策略，全部停用，包含不可变版本历史，不直接删除业务表记录。

本轮应用未作业务逻辑或视觉调整。构建网络调整使用腾讯云 Debian/PyPI 镜像，保留 APT 签名及所有 95 个锁定包的版本/hash；固定版本 uv 与独立构建依赖使用选定索引。官方 SDK GitHub 下载超时时使用经校验的原 commit Git bundle 缓存，流程见 [生产规则](../runbooks/production-junbo.md)。

## 备份与恢复证据

- 首份备份：`/opt/tt-ada/backups/20260910T075534Z-first-release`，具有 COMPLETE，全部文件 SHA256 校验通过。
- 实际恢复至本次新建的 `tt_ada_restore_test_20260910075605`，使用容器内 PostgreSQL 18 的 `pg_restore --exit-on-error --no-owner --no-acl`。迁移 head 相同，恢复时的用户 2、租户 1、成员 1、策略 2、策略版本 3 与生产库一致。恢复验收结束后只删除该临时库。此备份早于第三条 UI 复验策略，不用后续数量覆盖当时恢复证据。
- `tt-ada-backup.service` 手动触发成功（Result=success、ExecMainStatus=0），生成 `/opt/tt-ada/backups/20260910T075705Z-scheduled`；摘要校验通过，包含复验后的数据。
- `tt-ada-backup.timer` 已启用，每日北京时间 03:30 加随机延迟；安装时下一次为 2026-09-11 03:30:57。所有备份文件实际权限 0600，目录 0700。
- Redis RDB 通过 `redis-check-rdb`；没有在生产卷执行 Redis 恢复。AOF/RDB 的恢复限制与 PostgreSQL/outbox 核对要求写入规则。
- 当前备份在同机，异机副本尚未配置；主机/磁盘整体故障的恢复不在本次成功证据内。

## 外部接入边界

基础应用已可用，真实投放链路尚未联调：

- TikTok App、BC 授权、版权方账号和 R2 桶/凭据未配置；素材新导入和自动清理开关保持 false。未发出真实授权、取链、视频上传、广告创建或投放请求。
- 未连接 BC 时，素材/搭建/任务页面显示接入引导；这些状态不是完整投放已可用的证据。
- 无凭据直连 `https://business-api.tiktok.com/` 两次出现连接超时。末次 DNS 解析约 0.013 秒，但 TCP/TLS 未建立，10 秒连接超时、HTTP=000。需要在真实授权前解决该主机到 TikTok API/媒体服务的出站连接，或另行采用网络可达的部署环境；本次没有更改整机网络代理。
- 当前证书覆盖域名且有效至 2026-12-18，沿用统一证书续期流程。带 8000 的 HTTPS 回调入口可访问，但 TikTok 开发者后台是否接受此回调及真实 OAuth 成功尚未验证。
- 约 835 MiB 的容器内存为基础空载时单次采样；没有验证生产批量视频和广告吞吐，不能据此宣称容量达标。

后续发布必须先读 [生产环境与发布规则](../runbooks/production-junbo.md) 及仓库 AGENTS.md。操作顺序包括固定 SHA、构建、暂停定时备份、停写和正常排空、备份、迁移、验收、切换 current、恢复定时备份和记录发布；真实外部功能按其专门清单接入。

## 证据存放

浏览器截图和脱敏 JSON 保存在开发机忽略目录 `.runtime/production-ui-2026-09-10T07-56-20-568Z/`（完整复验）与 `.runtime/production-ui-2026-09-10T07-58-50-944Z/`（重启后只读复验）；不提交登录会话。服务器 `/opt/tt-ada/production-api-acceptance.json`、`production-restore-acceptance.json` 保留 API/恢复证据。密码、私有环境配置、数据库归档和运行文件不进入 Git。
