# 2026-09-29 账户多关键词搜索——新加坡测试服发布

- 用户明确授权推送并发布。代码 `6ef4661e58cf99d212ce051f5c3b37324fd8ea14` 已推送并替换原运行代码 `f0317c89151f2019cfe1b82cb24a22baed50041e`；账户名称按空白分词、全部包含、不限顺序和大小写，完整 ID 保持精确匹配。账户管理、选择弹窗、计数、分页及全选共用服务端规则。
- 本地账户路由 17 项、浏览器 3 项、Python 语法 / Ruff / Biome / TypeScript / Vite 构建通过。固定 Git archive 与前端产物在服务器校验 SHA-256，无迁移、依赖锁或环境配置变化，数据库 current/head 保持 `audience_targeting`，Alembic check 通过。
- 四 Worker active/reserved 为空后停 API / Beat，正常排空四 Worker 并暂停备份 timer。完整备份 `/var/backups/tt-ada-staging/20260929T015950Z/` 包含 PostgreSQL、Redis、独立项目 / 前端构建、私有配置 / 证书 / systemd / Nginx / MCP 注册与备份脚本，五归档校验通过。
- 隔离恢复核对 1,434 个项目文件、Redis 5 个 key、22 张业务表摘要、10,484 份加密响应的解密 / 长度 / SHA-256；通过后才切换。演练临时数据库和目录验证后清理，原版本和完整备份保留，异地备份仍未配置。
- API / 四 Worker / Beat 共 11 个实际进程同版本、配置摘要一致；原入库和清理开关保持 true，调用额度 / 媒体白名单 / 数据与授权配置不变，`app.env` 摘要一致。六服务 active、NRestarts=0；四 Worker 并发 2/1/1/1，四队列为 0，备份 timer 恢复。
- HTTPS 健康、实际 HTML / `BuildInputPage-stwvLEQu.js` 与本地产物字节一致、平台和租户管理员登录与权限隔离、未登录账户接口 401、未知 API 404 均通过。
- 通过实际测试站点 HTTP 接口读取既有本地账户目录，取已有名称首尾关键词倒序并用双空格连接，以每页 2 条验证 42 个匹配账户与完整目录计算结果一致。另验证 `MAX P1` 和 `p1 max` 结果集合相同；抽查目录中两者均为 0 项，不将空结果描述为真实 MAX / P1 账户命中。正向匹配与分页由上述 42 项验证。
- 未发起 TikTok 同步、版权方操作、广告创建或恢复。本次仅为测试环境发布与本地目录读取验收。
