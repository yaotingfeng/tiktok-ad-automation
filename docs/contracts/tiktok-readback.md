# TikTok 双通道创建与严格回读合同

本文描述当前后端代码合同及离线证据，不代表真实 TikTok 授权、上传或广告联调已通过。官方 SDK 固定版本、MCP 公共元数据与候选工具来源分别见 [MCP 协议记录](../integrations/tiktok-mcp-protocol.md)和锁文件；本轮未重新访问官方服务。整体验收状态见 [离线验收记录](../validation/2026-09-11-tiktok-dual-channel-offline.md)，真实联调保持 [未执行](../acceptance/live-mcp.md)。

## 通道与冻结归属

业务只调用 `open_tiktok_gateway(...).builds` 的类型化 `BuildOperations`；API 使用官方 SDK，MCP 使用官方 MCP SDK 和固定端点。不存在模型决策、提示词执行、自建 MCP server 或失败后换通道的补建路径。

`FrozenTikTokRoute` 保存 tenant_id、bc_id、connection_id、channel、authorization_revision、adapter_contract_revision。新准备开始时解析一次明确连接或 BC 默认连接；已有预览重入、提交、展开、CTA、素材与三级广告从父记录继承原 route。草稿准备与新预览各自是新的准备入口；已冻结预览不会跟随今日默认。来源素材另存其真实来源账户及 source_route，不能用目标连接替代来源授权。

正常凭据轮换不改变 authorization_revision，不使冻结任务过期。授权语义、业务权限或适配合同变化会阻断原路由；每次物理 HTTP（含握手后的业务调用）重新核实当前 actor 权限、tenant/BC/账户/连接、claim、绝对期限及共享准入。远端返回后再次核实发布条件；迟到响应只能追加证据，不能覆盖新 owner。

## 创建与发送证据

提交具体预览后，Campaign、Ad Group、Ad 直接按 `ENABLE` 创建，不增加二次激活或账户轮转。已核实可准备的 PREPARING 组合可提交，但实际广告创建必须等待目标视频和封面全部完成并核实；明确 BLOCKED 的依赖不能提交。

CTA 有独立 attempt。每次广告创建先保存精确 wire body、摘要和原 attempt 归属；SDK/MCP 各自序列化，不能保存一种正文却发送另一种。MCP Campaign/Ad Group 的必需 request_id 使用已持久化的 attempt UUID；它只是关联标识，**不是已核实的幂等或安全重放保证**。API 不添加该 MCP 字段。动态时间只在首次 arm 生成，安全重排保留原正文、摘要与 attempt。

| 结果证据 | 行为 |
| --- | --- |
| 明确 NOT_SENT，且属于当前原 attempt/lease、没有未知或迟到副作用证据 | 按准入退避重排；保留同一 attempt/body/request_id，以新 nonce 围栏旧 worker |
| 已发送后断流、超时、进程死亡或无法证明无副作用的业务错误 | UNKNOWN；不再次 create，不换 token/连接/通道补建 |
| 返回可验证的已知对象 ID | 先持久提交回执，再关闭客户端；清理失败不能丢弃 ID 或重建 |
| 多个互相冲突的 ID | 保留所有证据，阻断自动择一 |

HTTP 200、MCP isError=false、自然语言成功和非零业务码均不能单独证明远端副作用。仅消费已固定并验证的业务 envelope；未知服务端重试语义不转换成安全重放。日志和公开投影仅含白名单 request_id/mcp_request_id/remote_task_id/attempt_id、状态与归属，不包含 token、原始响应或签名 URL。

## 读取操作与严格事实

| 对象 | 官方 SDK 方法 | MCP 候选工具（必须由实际连接核实 schema） |
| --- | --- | --- |
| Campaign | [CampaignCreationApi.smart_plus_campaign_get](https://business-api.tiktok.com/portal/docs?id=1843312818332930) | smart_plus_campaign_get |
| Ad Group | [AdgroupApi.smart_plus_adgroup_get](https://business-api.tiktok.com/portal/docs?id=1843314879617026) | smart_plus_adgroup_get |
| Ad | [AdApi.smart_plus_ad_get](https://business-api.tiktok.com/portal/docs?id=1843317378982914) | smart_plus_ad_get |
| Ad Group 状态补查 | [AdgroupApi.adgroup_get](https://business-api.tiktok.com/portal/docs?id=1739314558673922) | adgroup_get |
| 已知 CTA portfolio | [CreativeManagementApi.creative_portfolio_get](https://business-api.tiktok.com/portal/docs?id=1739092113671170) | creative_portfolio_get |

`read_page(query=BuildReadQuery(...))` 返回 `BuildPage`，`read_adgroup_status(...)` 返回 `AdGroupStatus`。共同记录真实 `CallEvidence`。候选工具出现不代表有当前写权限；连接实际 observation 与固定合同均通过后才可准入相应操作。

预期值只来自不可变的 request_body 与正确摘要。MCP Campaign/Ad Group 解码前，必须按原 REQUEST_ARMED 证据找到唯一原创建 attempt，并核实 request_id 与该 UUID 完全相同；随后仅在用于比较的副本中剥离该关联字段。不能用后来核查 attempt、最新草稿或请求字段补造远端事实。

回读必须覆盖实际账户、父级、精确名称、Campaign 预算/CBO/目标、Ad Group ROAS/Minis/地区/排期/优化与计费字段，以及 Ad 的目标视频/封面、身份、BC、文案、URL、CTA portfolio。Smart+ Ad ID 保留 `smart_plus_ad_id`，不得用普通 Ad ID 混淆。集合顺序不重要，成员和重复数量仍参与比较。

原始远端 JSON 金额使用精确十进制解码；不能先经过 SDK float 舍入再比较，不能以请求值恢复远端精度。历史输入兼容与远端事实解码分开；非有限值、损坏类型或精度不明保持未核实。已明确返回非 ENABLE 时优先记录状态 MISMATCH，即使另有缺项，也不能被 INCOMPLETE 掩盖，更不会发送状态修改。ENABLE 仅是观察到的配置，不代表审核通过、开始投放或产生消耗。

`BuildRecord.intent=None` 与 `missing_fields` 保留缺损事实。Smart+ Ad Group 只缺 operation_status 时，保存其余完整的类型化 observed_adgroup；另一 delivery 经原 route 用实际 ID 读取普通 adgroup 状态，身份/账户/ROAS一致后才能合并实际事实。CTA 仅支持已知 portfolio ID 的读取，不发明未知 ID 搜索；**响应缺 advertiser_id 时仍为 INCOMPLETE，不能从请求账户补值**。已知 CTA ID继续保留，不能宣称完整 MATCH。

## 分页、调度与恢复

名称过滤可能模糊，必须本地精确比较。Campaign/Ad Group 按原父级查询；Ad 有已知 ID 时按实际 smart_plus_ad_ids，否则按原 adgroup_ids 搜索。每页最多 100 项，核实 page/page_size/total_page/total_number、一致总数及跨页无重复 ID。空页不是未创建证明；无已知 ID 时仅完整稳定分页内唯一且完整匹配的对象可确认。公共接口没有已核实的原子快照保证，总数改变、重复项、缺字段或多候选均保留 UNKNOWN。

`process_reconciliation(..., step_id, revision)` 每次 delivery 最多一个业务只读操作，返回 `ReconciliationResult`，由持久 outbox 调度下一页或补查。相同 revision 的已保存结果重入不再发送；未发布 outbox 修复保留原 dispatch/revision。`needs_more` 仅表示安排下一次读取，不授权 create。终态 UNKNOWN 后用户可以显式发起新的核查。

实际 worker 要求 Linux prefork，广告读取硬期限至多 45 秒；数据库等待、握手、共享准入、业务 HTTP 与清理使用同一绝对期限。任务 lease 覆盖执行和清理；不能用 socket timeout 代替进程终止保证。macOS 离线通过不证明 Linux hard-kill 场景已经验收。

普通核查使用原冻结授权。重新授权后仅在同 tenant/BC/connection/channel、已验证同一主体及 issuer/resource、当前明确读取权限且原创建证据完整时，允许独立的历史只读审计。它保存新旧授权版本，**不修改原 route/step/body，不唤醒原广告创建**。丢失 POST 回执时只凭原 request UUID GET 查询；PENDING/RUNNING、回执丢失或 GET 404 均不得自动再 POST。已取得并验证 read_id 的明确 UNKNOWN/BLOCKED 终态，且当前管理权限和服务端资格均恢复后，用户才可点击“再次只读核查”生成新 UUID。

历史缺 route 或无法证明 authorization_revision 的请求保持不可自动执行；不补今日默认、当前授权或猜测的 BC。旧事实、请求正文/摘要和已知 ID 保留。迁移与恢复约束见 [恢复手册](../runbooks/recovery.md)。

## 本地验证范围

本轮完整后端矩阵 2203 passed、9 skipped，其中 8 项 Linux prefork 未执行、1 项为 API 参数不适用；前端 workspace 348 passed。金额/父子/素材事实、UNKNOWN 保护及迁移证据均由本地 PostgreSQL/Redis 与合成 HTTP 验证，实际 MCP 注册、主体/scope、服务策略和生产环境未验。精确版本、命令与首次失败后的修复结果见 [离线验收](../validation/2026-09-11-tiktok-dual-channel-offline.md)。
