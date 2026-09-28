# 2026-09-28 账户选择弹窗精简与对齐——测试环境发布

- 用户明确授权推送并发布到新加坡测试服务器。代码提交 `f0317c89151f2019cfe1b82cb24a22baed50041e` 已推送 `origin/main`，替换原运行代码 `ec45538eedd7aeee7f43156525555f5921c538d9`。
- 删除账户选择弹窗的平台状态输入与查询参数，保留名称 / ID 搜索、可用性筛选；桌面端并排、窄屏堆叠，全选操作垂直居中并对齐列表边距。发布前相关 3 项浏览器测试、1440px / 390px 对齐检查、TypeScript / Vite 构建及 Biome 通过。
- 固定 Git archive 源码包和本地生产前端包传输后均核验 SHA-256；后端源码与依赖锁未变，复用现有可重建 Python 环境。无数据库迁移，current/head 保持 `audience_targeting`，Alembic check 无模型差异。

## 备份与切换

- 四个 Worker active/reserved 均为空；停 API/Beat 并正常排空四 Worker，暂停备份 timer，未清空队列或 Outbox。
- 完整备份 `/var/backups/tt-ada-staging/20260928T104355Z/` 包含 PostgreSQL、Redis、原项目与实际前端构建、私有配置 / systemd / Nginx / 证书 / MCP 注册及备份脚本；五份归档 SHA-256 均通过。
- 独立恢复核对 1,433 个项目文件；Redis 独立 Unix socket 实例载入并读取 5 个 key；PostgreSQL 隔离库验证 22 张业务表摘要和 10,484 份加密响应的解密 / 长度 / SHA-256，一致后才切换。恢复演练临时库和目录验证后清理，原版本与完整备份保留；未配置异地备份。
- 预检忽略旧归档中的 macOS `._*` 附属文件后通过后端差异检查，预检失败时尚未停服务。解包的 macOS 扩展属性警告不影响文件内容与摘要。

## 发布后验收

- `current`、API / Resource / Result / Build / Control / Beat 的全部 11 个实际进程统一为 `f0317c8`；配置摘要发布前后一致，`app.env` 摘要不变。`MATERIAL_INGEST_ENABLED=true`、`MATERIAL_CLEANUP_ENABLED=true` 保持，调用额度、媒体白名单、数据库 / Redis / 加密 / 集成配置未变。
- 六服务 active、NRestarts=0；四 Worker 进程池并发保持 2/1/1/1，四业务队列均为 0，备份 timer 已恢复。
- HTTPS 健康通过，实际登录 HTML 和 `BuildInputPage-esUveM0h.js` 与发布产物逐字节一致；新版资源不再包含平台状态输入，包含对齐样式及两项全选操作。两类管理员真实登录、profile、租户访问及平台权限隔离通过，未登录账户接口 401、未知 API 404。
- 初次 HTTP 验收未声明 `Accept: text/html`，登录路由按 API 请求处理；按浏览器请求补齐该头后通过。该调整仅涉及验收脚本，运行代码未变。
- 未调用 TikTok / 版权方接口，未创建、恢复、重试或启用广告。本次为测试环境 UI 发布，不代表真实广告业务联调。
