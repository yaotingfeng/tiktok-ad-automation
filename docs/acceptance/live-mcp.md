# 官方 MCP 真实联调验收表

**状态：已完成测试站点客户端登记，租户与业务真实联调未执行。** 2026-09-12 官方注册返回 HTTP 201，部署配置与管理员页面 READY 已核实，见 [测试发布记录](../validation/2026-09-12-staging-mcp-release.md)。未发起租户 TikTok 登录同意、查询真实 BC、上传 TikTok/R2 素材或创建广告。原本地后端 2203 passed、前端 348 passed，以及本次 Linux 9 项验收，均不是业务真实联调证据。

## 执行前证据

操作者先选择实际目标环境、租户和 BC；不得由历史账号 ID 猜测 BC，不借用 Codex 连接/token。后端只连接固定官方 MCP 端点。租户管理员自行完成授权并明确绑定 BC；API App 是 API 通道配置，不能作为 MCP 授权的必需条件。开发合成注册、主体、scope、工具 schema、上传策略均不得带入实际环境。

| 验证项 | 必须保存的非秘密证据 | 当前状态 |
| --- | --- | --- |
| 客户端注册 | 官方注册条件、实际 client_id 审计引用、精确 callback URI、issuer/resource 与固定 profile 一致 | 2026-09-12 测试站点实际 HTTP 201；私有材料与精确 callback 已校验，见发布记录 |
| OAuth/PKCE | 当前 tenant_admin、state 单次持久 claim、S256、实际同源回调、受控凭据存储和失败清理 | 未执行 |
| 授权主体与权限 | 实际返回主体、scope、issuer/resource、当前用户及账户角色/权限来源、完整目录分页 | 未执行；mcp:tt4b/工具存在不能证明 write |
| 连接与 BC | 后台 connection_id/channel、明确 BC binding、默认连接选择、其他 tenant/BC 的拒绝证据 | 未执行 |
| 实际 MCP 协议 | 协商版本、完整 tools/list、input/output schema 与固定 revision 匹配、严格 envelope | 未执行；只有离线候选工具声明 |
| 刷新 | 同一刷新链路 CAS、正常凭据轮换不变授权版本、pending 等待、UNKNOWN 不重送旧 refresh token | 未执行；不假造 grant_id，不声明服务重放保证 |
| 远端 revoke | 证明 grant 隔离及对其他连接的影响 | 未核实，当前明确不支持远端 revoke；本地 disable 另验 |
| 当前授权围栏 | 握手后撤权、请求前后失权、claim 被抢时零新业务发送/旧 worker 不回写 | 只有合成离线证据 |

## MCP 视频 URL 入库五项独立证据

以下五字段属于代码审查管理的 `MaterialUploadPolicy`，须绑定 channel 和 adapter_contract_revision。用户设置、UI 勾选、工具接受参数或客户端不重试不能替代服务侧证据；测试中的 SYNTHETIC 策略不开放生产能力。

| 字段 | 必须核实的含义 | 当前真实状态 |
| --- | --- | --- |
| max_bytes | 官方服务允许的正整数容量，不能套用应用设置上限 | 未核实 |
| identity_verified | URL 拉取后不可变内容身份可独立证明，与源原件 digest/大小一致 | 未核实 |
| no_auto_fix_verified | 服务确实不自动改写上传内容 | 未核实 |
| no_auto_bind_verified | 服务确实不自动绑定业务对象 | 未核实 |
| unsafe_server_retry | 必须明确 False；无未受控服务重试造成重复副作用 | 未核实 |

五项未齐全时，MCP 新视频入库保持 `material_channel_unverified`，不能以离线全链通过解锁。API 既有上传路径沿自身已批准合同及实际强回读，不把 API 客户端序列化当成上述 MCP 服务保证。图片/封面也须独立核实实际上传返回与详情字段；图片能力不能证明视频入库能力。

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
2. 五项视频证据均通过后，批准指定原件的来源上传及目标跨账户准备，记录真实源/目标事实。未知上传保留原件；已知 VID 但 digest/大小不完整也不能释放 OriginalUse。
3. 审阅并提交具体预览；PREPARING 仅在已验证可准备时可提交。目标视频或封面未完成时必须零广告 create；依赖 READY 后逐层直接 ENABLE。
4. 每个 CTA/广告 attempt 最多一次发送；回执先持久化再清理。逐层独立回读。空页、缺字段、文本成功、HTTP 200 不等于 MATCH。
5. 在获准的专用测试环境做故障注入，记录接收后断线/worker 终止后 create 数仍为 1，原通道只读恢复。不得在真实投放账户擅自注入故障。
6. Linux prefork 已在 2026-09-12 测试服务器实际执行，8 个原跳过项目及 1 个相关用例通过，见发布记录；使用合成平台传输，不能替代真实 TikTok 故障联调。

任何主体/BC 不符、schema 漂移、未核实写权限、缺少五项服务能力、历史 UNKNOWN 无可证原 route 或重复远端 ID 都停止新写，保留证据后按 [恢复手册](../runbooks/recovery.md)处理。真实联调通过也不自动授权生产发布，发布须遵循目标环境手册。
