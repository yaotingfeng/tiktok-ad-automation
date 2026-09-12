# 测试环境 MCP 授权后读取修复（2026-09-12）

## 实际排查与范围

- 首次尝试停留 PENDING，未兑换令牌；第二次用户授权在 UTC 06:59:43 进入 CANDIDATE_READY，候选令牌已加密保存。此次错误位于候选 BC 读取，不能再报告为 OAuth 未成功。
- TIKTOK_CALL_POLICIES 缺失产生 admission_unconfigured。已于测试服务器补齐独立本地限流，所有未知 MCP 主体共用 shared-unverified 域；具体值见测试手册。官方每用户/工具 3 QPS 与本地总量/并发限制分开记录。
- 官方 initialize/tools/list 可以请求；实际 7 个账户 schema 与源码表示存在空 properties 省略、number 的 double 注解差异，28ba66f 修复后通过比较。BC 请求进一步触发 RemoteCallError，错误处理因缺少 502 消息又抛 KeyError，导致界面无法获得具体错误码。
- 补齐 502 通用脱敏提示；依据 [官方自定义客户端指南](https://business-api.tiktok.com/portal/docs/how-to-connect-a-custom-agent-to-tiktok-for-business-mcp-server/v1.3) 为 accounts.* 的 7 个只读工具开启单条 JSON TextContent envelope。继续严格要求 code=0、数据形状、无歧义回执；非零 code 不解释为成功。没有放开广告/素材写入合同。
- 同步 manifest SHA；共用观测查询仅复用当前合同版本，旧观测必须重新 tools/list 后才能读取/绑定。已有冻结任务不改路由。两轮 gpt-6-astra high 独立只读审查完成，观测缓存问题已修复并复核通过。

## 验证

- 6 个真实 schema 表示差异回归先失败，修复后协议 50 passed；7 个官方脱敏账户 schema 离线逐项比较通过。28ba66f 在测试服务器再次运行协议 50 passed。
- 502 两例先复现 KeyError，修复后错误响应 8 passed；JSON 文本账户响应两例先失败，修复后协议与响应解析合计 120 passed。
- 新增真实 PostgreSQL/Redis 回归：旧 observation revision 必须阻断绑定，重新读取会请求 tools/list，新绑定引用当前 SHA 的新 observation。先失败，修复后通过。
- 首次隔离运行通信回归因未载入父级 Redis fixture 出现 3 个初始化错误，其余 168 passed；后续改用独立测试数据库及 Redis DB14/15 运行完整相关集，最终七文件 253 passed（89.35 秒），Ruff、显式虚拟环境 ty、git diff --check 通过。没有把初始化错误记作通过。

## 备份与发布

- 调用限流配置发布前：`/var/backups/tt-ada-staging/20260912T070256Z/`。
- 28ba66f 代码发布前：`/var/backups/tt-ada-staging/20260912T070835Z/`。两个批次均包含 PostgreSQL、Redis、完整项目归档（含 backend/app/frontend 已构建产物）及私有配置；PG 临时新库、Redis 独立 socket 实例、项目隔离解压和 SHA-256 均验证，RELEASE_COMPLETE 已写入。
- API/Worker/Beat 同一 current 路径；仅后端及合同修改，前端产物与依赖未变化，固定版本复用已验证前端及原虚拟环境。保留依赖所指向的旧 release，不得清理。
- 不替用户绑定 BC、设置默认连接、上传素材或创建广告。候选选择窗口过期后需用户重新授权，不能修改数据库过期时间或借用 Codex 凭据绕过。
