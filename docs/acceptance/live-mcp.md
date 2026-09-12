# 官方 MCP 真实联调验收表

**状态：测试站点已完成客户端登记、真实 OAuth/PKCE 兑换、BC 读取、用户确认绑定和账户发现。** 2026-09-12 在用户指定的 Sun Browser 中重新授权并绑定已确认 BC，连接 ACTIVE、发现 COMPLETE，42 个真实广告账户已显示，币种/时区/平台状态已取得，见 [真实读取验收](../validation/2026-09-12-staging-mcp-pagination.md)。2026-09-13 已完成指定账户的真实 URL 素材上传与原账户回读核验，页面已入库 1、失败 0，见 [素材验收](../validation/2026-09-13-mcp-material-upload.md)。搭建权限及广告创建仍待单独验收。

## 执行前证据

操作者先选择实际目标环境、租户和 BC；不得由历史账号 ID 猜测 BC，不借用 Codex 连接/token。后端只连接固定官方 MCP 端点。租户管理员自行完成授权并明确绑定 BC；API App 是 API 通道配置，不能作为 MCP 授权的必需条件。开发合成注册、主体、scope、工具 schema、上传策略均不得带入实际环境。

| 验证项 | 必须保存的非秘密证据 | 当前状态 |
| --- | --- | --- |
| 客户端注册 | 官方注册条件、实际 client_id 审计引用、精确 callback URI、issuer/resource 与固定 profile 一致 | 2026-09-12 测试站点实际 HTTP 201；私有材料与精确 callback 已校验，见发布记录 |
| OAuth/PKCE | 当前 tenant_admin、state 单次持久 claim、S256、实际同源回调、受控凭据存储和失败清理 | 2026-09-12 Sun Browser 重新授权成功，候选经目录核验正式发布 |
| 授权主体与权限 | 实际返回主体、scope、issuer/resource、当前用户及账户角色/权限来源、完整目录分页 | SUBJECT/AUTHORIZED/BCS/ASSETS/DETAILS/ROLES 六阶段真实读取及发布通过；上传权限经实际 scope 和账户 ADMIN 角色核实；搭建权限仍待验 |
| 连接与 BC | 后台 connection_id/channel、明确 BC binding、默认连接选择、其他 tenant/BC 的拒绝证据 | 用户确认后绑定一个 BC，ACTIVE；无既有默认时自动初始化默认路由，未覆盖已有默认。隔离拒绝保留合成及本地权限测试证据 |
| 实际 MCP 协议 | 协商版本、完整 tools/list、input/output schema 与固定 revision 匹配、严格 envelope | 实际 initialize/tools/list 及六个目录读取阶段成功；账户文本 JSON 回执通过固定合同解析 |
| 刷新 | 同一刷新链路 CAS、正常凭据轮换不变授权版本、pending 等待、UNKNOWN 不重送旧 refresh token | 未执行；不假造 grant_id，不声明服务重放保证 |
| 远端 revoke | 证明 grant 隔离及对其他连接的影响 | 未核实，当前明确不支持远端 revoke；本地 disable 另验 |
| 当前授权围栏 | 握手后撤权、请求前后失权、claim 被抢时零新业务发送/旧 worker 不回写 | 只有合成离线证据 |

## MCP 视频 URL 入库（2026-09-13 更新）

用户要求接通已实际使用的 URL 上传，当前规则见 [修复记录](../validation/2026-09-13-mcp-material-upload.md)。原五项未知服务内部保证不再作为永久关闭上传的静态条件，不宣称这些内部行为已获证明。

执行前核对真实授权 scope、主体、已绑定 BC、完整账户目录及操作角色、当前接口版本、工程容量和调度租约。上传请求关闭自动修复/绑定，已发送或结果未知不重放；回执必须持久化，并在原账户核对 VID、MD5 和大小后才标记成功。缺项、冲突或回读不完整时保留原件并只回查。

## 具体一批的授权记录

每次真实联调另建**实际执行日期**的 `docs/validation/YYYY-MM-DD-tiktok-mcp-live.md`，通过审阅的具体预览再执行。受控证据中保存实际 ID；公开报告需要脱敏，不保存 token、cookie、授权 code/state、签名 URL、原始 OAuth 响应或凭据文件。

| 字段 | 本次值 |
| --- | --- |
| 日期、操作者、目标环境、入口 | 未执行 / 待填 |
| Git SHA、镜像 digest、数据库 head | 待填，不采用开发工作区状态 |
| tenant / BC / connection_id / channel | 待填；必须明确选择 |
| 原 route / authorization_revision / adapter_contract_revision | 待填 |
| 客户端注册、主体/scope、具体操作批准的审计引用 | 待填；未批准不得继续 |
| 源账户 / 实际 VID / MID / 原件 generation / MD5 / 大小 | 待填 |
| 目标账户 / 实际 VID / MID / cover image ID | 待填；不得复制源 ID |
| 剧目、完整目标账户列表、预览 ID | 待填；所有剧目投全部所选账户 |
| CTA 与三级冻结名称、预算、ROAS、父级、ENABLE | 待填；无默认预算或试投代填 |
| request UUID / attempt UUID / request_id / mcp_request_id / remote_task_id | 待填，逐请求记录 |
| 每层远端对象数量及具体 ID | 待填；不能用成功文案代替 |
| 独立回读结果、缺项、状态、父子/金额/素材比较 | 待填；CTA 缺 advertiser_id 保留 INCOMPLETE |
| UNKNOWN / 原通道读取 / 新授权独立审计结果 | 待填；不可借核查补建 |
| 原件用途结束依据/清理条件与实际存储核对 | 待填 |

## 必测流程与停止条件

1. 管理员授权后完整读取账户/场景，验证 BC/主体/权限；viewer 与其他 tenant/BC 请求必须拒绝。
2. 账户权限及请求检查通过后，按用户指定原件执行来源上传及目标跨账户准备，记录真实源/目标事实。未知上传保留原件；已知 VID 但 digest/大小不完整也不能释放 OriginalUse。
3. 审阅并提交具体预览；PREPARING 仅在已验证可准备时可提交。目标视频或封面未完成时必须零广告 create；依赖 READY 后逐层直接 ENABLE。
4. 每个 CTA/广告 attempt 最多一次发送；回执先持久化再清理。逐层独立回读。空页、缺字段、文本成功、HTTP 200 不等于 MATCH。
5. 在获准的专用测试环境做故障注入，记录接收后断线/worker 终止后 create 数仍为 1，原通道只读恢复。不得在真实投放账户擅自注入故障。
6. Linux prefork 已在 2026-09-12 测试服务器实际执行，8 个原跳过项目及 1 个相关用例通过，见发布记录；使用合成平台传输，不能替代真实 TikTok 故障联调。

任何主体/BC 不符、schema 漂移、未核实写权限、请求或回读合同不完整、历史 UNKNOWN 无可证原 route 或重复远端 ID 都停止新写，保留证据后按 [恢复手册](../runbooks/recovery.md)处理。真实联调通过也不自动授权生产发布，发布须遵循目标环境手册。
