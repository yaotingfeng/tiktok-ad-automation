# 广告目录与报表接口合同证据

核对日期：2026-09-30。用于广告管理与报表开发；以下为官方文档、固定 SDK 源码及历史 MCP schema 的合同证据，不代表当前连接权限或真实账户联调通过。MCP/API 实际验收分别记录。

## 目录与身份

普通接口的 `campaign_automation_type` 区分 `MANUAL`、`SMART_PLUS`、`UPGRADED_SMART_PLUS`；普通广告接口还返回 `UPGRADED_SMART_PLUS_CREATIVE`。广告集合的 `smart_plus_ad_id`/`ad_id_v2` 与创意 `ad_id`、广告内 `ad_material_id`、素材库 `video_id` 分别保存，不相互替代。

依据：[普通系列](https://business-api.tiktok.com/portal/docs/get-campaigns/v1.3)、[普通广告](https://business-api.tiktok.com/portal/docs/get-ads/v1.3)、[Upgraded Smart+ 接口目录](https://business-api.tiktok.com/portal/docs?id=1843301794874370)。

普通和 Upgraded Smart+ 的系列、组、广告查询均将 `primary_status` 指向官方状态枚举，`STATUS_ALL` 包含已删除对象。不能仅因 MCP schema 描述未列举枚举，就把完整查询判为不支持。默认查询可能排除已删除对象；缺失记录也不能作为删除证明。

依据：[状态枚举](https://business-api.tiktok.com/portal/docs?id=1737174886619138)、[主次状态关系](https://business-api.tiktok.com/portal/docs?id=1757239620352002)、[Smart+ 系列](https://business-api.tiktok.com/portal/docs?id=1843312818332930)、[Smart+ 组](https://business-api.tiktok.com/portal/docs?id=1843314879617026)、[Smart+ 广告](https://business-api.tiktok.com/portal/docs?id=1843317378982914)。Smart+ 系列和组单页最多 1,000 条，广告最多 100 条。

Smart+ 广告读取不包含所有自动添加创意，需要普通广告目录补充。普通接口按 Upgraded Smart+ 的 `ad_ids_v2` 过滤时只返回有限字段，不能当作完整创意内容；只能通过实际返回的关联 ID 连接。只有帖子标识的 Spark 素材保存真实 `tiktok_item_id`，使用内部 `TIKTOK_POST` 类型与视频库 ID 区分；不由此推定原生素材报告或管理能力。素材列表为空时仍须单独记录完整性，不能把补读失败解释为没有素材。依据：[普通广告](https://business-api.tiktok.com/portal/docs/get-ads/v1.3)、[Smart+ 广告](https://business-api.tiktok.com/portal/docs?id=1843317378982914)。

## 余额与财务权限

余额是 BC 财务读取，响应为 `data.advertiser_account_list[]` 和 `page_info`。`balance_info` 提供广告账户明细；查询附加字段需 `page_size=1`。其中 `account_balance` 是账户总余额，`valid_account_balance` 是可用余额，两者不能混用。

启用 Payment Portfolio 的账户，顶层部分余额字段可能代表共享资金池。保存 `ADVERTISER`、`PORTFOLIO` 或 `UNKNOWN` 范围及来源 ID；不得对多个账户重复累计同一资金池金额。缺失、无权限或无法确认范围时保留不可用，不能填零。依据：[广告账户余额](https://business-api.tiktok.com/portal/docs?id=1739939106470913)。

财务证据须把当前授权主体精确关联到同一 BC 中 `BOUND` 成员，并核验 `ext_user_role.finance_role` 为 `MANAGER` 或 `ANALYST`。广告读写权限或 BC 的普通 `ADMIN` 字段不能代替财务角色；成员查询本身也有权限限制。无法确认身份或读取证据时不探测余额。依据：[BC 成员](https://business-api.tiktok.com/portal/docs?id=1739939404802049)。

## 同步报告

基础报表按账户、系列、组、广告各自的原生层级读取。Upgraded Smart+ 集合使用 `ad_id_v2`，创意使用 `ad_id`；二者不能混合维度或过滤字段。按日最多 30 天、按小时最多 1 天；无时间维度的区间最多 365 天。每页最多 1,000 行，分页不能绕过同步报告的 20,000 广告对象上限。依据：[同步报告](https://business-api.tiktok.com/portal/docs/run-a-synchronous-report/v1.3)、[基础报告维度](https://business-api.tiktok.com/portal/docs/basic-reports-supported-dimensions/v1.3)。

核心原始指标包括 `spend`、`native_growth_ad_revenue_value_d0`、`native_growth_total_ad_impression_value`、`impressions` 和 `clicks`。D0 收入按点击后 24 小时归因解释，不等于自然日现金收入；D0 与总收入不相加。响应缺值或 `-` 保留不可用，真实零与缺失分开；比例由兼容的原始合计计算。账户层可包含竞价与合约消耗，不能假定总是等于子级竞价报告之和。具体合同开放矩阵以实现的指标字典为准，文档组合证据仍不是账户级实测。依据：[基础指标](https://business-api.tiktok.com/portal/docs/basic-reports-supported-metrics/v1.3)、[基础报告](https://business-api.tiktok.com/portal/docs/basic-reports/v1.3)。

## 素材报告

素材概览以一个广告层级与 `main_material_id` 组合，不支持时间拆分；breakdown 使用素材及支持的时间维度。原生素材身份应包含 `main_material_type`，不能统一等同于视频库 ID。概览可返回 `ad_material_id` 和创意 ID/名称关联。

已核实的素材指标包含消耗、展示和点击；没有从此合同验证 Native Growth 收入。该收入显示不支持，不能把父广告收入分摊到素材。广告/剧筛选与时间拆分的具体组合须经过合同核实，不用账户素材总额替代。主素材、文案、CTA 等可能重叠，不能混合后相加。

依据：[素材概览](https://business-api.tiktok.com/portal/docs?id=1843317489576961)、[概览维度](https://business-api.tiktok.com/portal/docs?id=1843337892165889)、[素材指标](https://business-api.tiktok.com/portal/docs?id=1843337909199938)、[素材拆分](https://business-api.tiktok.com/portal/docs?id=1843317510389761)、[拆分维度](https://business-api.tiktok.com/portal/docs?id=1843337879988226)。

## 异步报告

创建响应返回 `task_id`；状态查询使用 `QUEUING`、`PROCESSING`、`SUCCESS`、`FAILED`、`CANCELED`。只有 `SUCCESS` 可以下载，未知状态不能视为就绪。JSON 下载响应包括 `download_url`、`file_name`、`output_format`，文件链接有效一小时；刷新下载链接不应重新创建任务。

`CSV_STRING` 返回原始 CSV 流，列名使用展示文本；不能按 JSON envelope 解析，也不能假定所有 Native Growth 指标都有已知 CSV 表头。CSV/XLSX 的文件解析与业务指标映射须分别验证。签名 URL 不进入事实或公开错误信息。

异步能力有准入名单与独立指标/维度限制，同步支持不等于异步支持。创建受应用每秒及账户每小时限制，当前合同记录为 1 QPS/app、500 次/account/hour；实现还须保留真实上游配额域，新增队列不能扩大配额。创建结果不明不能盲目重建；已有任务恢复继续使用原任务号。

依据：[创建](https://business-api.tiktok.com/portal/docs/create-an-asynchronous-report-task/v1.3)、[状态](https://business-api.tiktok.com/portal/docs/get-the-status-of-an-async-report-task/v1.3)、[下载](https://business-api.tiktok.com/portal/docs/download-the-output-of-an-async-report-task/v1.3)、[异步工作流](https://business-api.tiktok.com/portal/docs/run-an-asynchronous-report/v1.3)。

## 管理写入的后续约束

普通广告组更新采用完整替换语义。只提交 ID 和 ROAS，或直接复制 GET 响应，都不能证明保留原配置；必须编译经过白名单核验的有效写入字段，不能证明时不发送。Smart+ 广告组出价更新可能联动同系列其他组，管理预览和互斥范围须包含真实影响上限。依据：[普通组更新](https://business-api.tiktok.com/portal/docs/update-an-ad-group/v1.3)、[Smart+ 组更新](https://business-api.tiktok.com/portal/docs?id=1843314894279682)。

## 代码与 MCP schema 来源

官方 Python SDK 固定版本由项目锁文件控制，核对源提交为 `f809c396520df2d7b201a9ccc5378d822b728ed3`。读取响应须保留业务 envelope 和原始十进制精度；SDK 的 `async_req` 仅是请求执行方式，不是平台异步报告任务。

本地保留的 2026-09-12 原始公开 `tools/list` schema 共 381 项，新增适配合同从原始 `inputSchema` 提取；抽核现有 30 项固定合同全部匹配。此历史证据只支持离线实现，运行时仍须当前连接完整 `tools/list`、schema 摘要及冻结授权校验。不能从 REST 字段列表自行拼造一份所谓已观测 MCP schema。工具名对应依据：[官方工具目录](https://business-api.tiktok.com/portal/docs/available-tools-in-tiktok-for-business-mcp-server/v1.3)、[工具发现](https://business-api.tiktok.com/portal/docs/use-mcp-tools/v1.3)。

MCP manifest 摘要变化会影响旧冻结路由的兼容校验。发布前必须核对新工具观察、授权和在途任务；不能重写旧冻结路由，或把历史 schema 匹配冒充当前连接就绪。

本文没有执行真实 TikTok 操作，不含业务凭据、真实报表或账户标识。开发测试、数据库并发验证及最终联调结果分别记录在各阶段验收文档。
