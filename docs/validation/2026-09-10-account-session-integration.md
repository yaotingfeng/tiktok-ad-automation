# 2026-09-10 账号、自动续登与视频上传计划交付

本轮实现普通账号密码登录和版权方自动续登，并交付独立 R2 视频上传设计、实施计划。R2 接收、URL 入库和自动删除尚未实施；不能把本轮测试计入该功能的实现完成度。

## 代码与文档

- `1102e26`：平台普通账号、邮箱及邮件找回移除、0015 身份保留迁移。详见[账号验证记录](username-auth.md)。
- `7b5b487`：网眼/嘉书按租户自动续登，持久重试与冷却，原业务操作继续且未知写入不重放。详见[续登验证记录](provider-auto-relogin.md)。运行时使用独立 Python 适配器，不调用版权方 CLI 或读取其账号文件。
- `7d2529b`：旧版本迁移测试改用独立历史数据库，继续验证索引、回填和往返；不跨越有意不可逆的邮箱删除迁移。
- `93942db`：迁移形成 `0014 → 0015_username_auth → 0016_provider_session_refresh` 的单一 head。
- `62e2812`：[R2 临时上传设计](../superpowers/specs/2026-09-10-r2-transient-video-upload-design.md)和[独立实施计划](../superpowers/plans/2026-09-10-r2-batch-video-upload-plan.md)，含 8 个任务、66 个待执行步骤。

历史素材上传设计已定位到工作区 `outputs/unclassified/20260904-r2-tiktok-speed-test/system-design.md`。新计划要求来源账户强回读成功后主动清理，并将“原件删除后向新目标分发及目标封面核实”设为启用清理的前置验收。

## 集成验证

在专用 `*_test` PostgreSQL 与独立非零 Redis 测试库执行，未使用业务库运行 pytest。

- 全量后端（排除独立 acceptance 目录）首轮：1388 passed、2 failed、1 skipped，534.54 秒。两个失败均为旧测试从 head 倒退穿过 0015；修复后的两个用例在合并分支重新执行，2 passed。没有新增业务代码失败，不将这两次运行误写成一次全绿运行。
- 唯一 skip 为 Linux prefork 硬期限测试，按既有平台条件在 macOS 跳过；需由 Linux CI 提供该证据。
- 全量 Ruff、Ty、包含 tests 的 TypeScript 检查与 Vite 生产构建通过。
- 合并后在本地生产静态页面运行版权方 Playwright：21 passed（8.6 秒）；覆盖自动恢复提示、租户权限、分页与凭据表单。这组 API 为边界替身。
- 账号子任务另有真实 API Chromium 37 passed、工作台全部 263 用例覆盖；具体命令、分开运行原因及数据库迁移回归见账号验证记录。

## 实际本地应用升级

应用位于 `projects/tiktok-ad-automation`，旧工作区快捷路径仍指向同一目录；本轮未覆盖工作区原有 15 份未提交文档，逐文件 SHA-256 核对一致。

1. 先对实际开发库做私有 custom-format PostgreSQL 备份，恢复到独立测试库，升级两次迁移并执行 Alembic check，核对旧身份及关系。
2. 停止确认为本项目的 API、Worker、Beat，再做最终备份和完整账号映射导出。备份/映射/环境均位于忽略的 `.runtime/account-session-change-baseline/`，目录 0700、文件 0600，不提交密钥或备份。
3. 根据映射把本地 `FIRST_SUPERUSER` 改为 `admin`，保留原密码。删除本系统废弃 SMTP 配置；确认无已有加密连接后生成本地私有连接加密密钥。
4. 实际开发库升级至 0016，Alembic check 无新增差异；用户 UUID、密码哈希、用户状态、租户、成员、审计及已有连接记录核对保留，user 表无 email 列。
5. 重启 API、Worker、单个 Beat，健康检查正常。API OpenAPI 确认 UserPublic 仅有 username 身份，邮件恢复/重置/测试邮件接口不存在。

通过用户可见浏览器操作，在 `http://127.0.0.1:8011` 退出旧登录，再使用 `admin` 和原密码登录成功，进入用户管理并打开新增用户表单（普通账号、密码、确认密码，无邮箱）。继续进入原租户、读取原策略 v2（USD 120、ROAS 1.08、10 素材/组、3 创意）以及版权方连接页面，确认自动恢复提示；未新建测试业务记录。

本地 Worker 保留 macOS solo 诊断方式。需要硬期限的生产业务任务仍要求 Linux prefork，不能据此宣称本机已具备真实广告执行验收。此轮没有使用真实网眼/嘉书/TikTok/R2 凭据，没有进行真实登录、上传、共享或广告创建。R2 账号、私有桶、部署配置及真实跨账户验收属于后续视频上传实施输入。
