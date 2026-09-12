## 2026-09-12：MCP 账户文本回执与候选版本修复

- 修复上游错误映射缺少 502 导致的二次异常；依据官方自定义客户端指南，为 7 个账户 READ 合同接受单条 JSON 文本回执，保持 code/形状/歧义检查，WRITE 门禁未放开。
- 更新合同 SHA，并按当前 SHA 查询候选工具观测；旧观测不能被绑定复用，读取可重新核实工具后继续。新增真实 PostgreSQL/Redis 回归先红后绿，gpt-6-astra high 独立复核 PASS。
- 完整相关七文件 **253 passed / 0 failed（89.35 秒）**，Ruff、ty 和 diff 检查通过。独立本地测试库，不使用业务数据库；外部 HTTP 使用传输层替身。
- 修复已以 `87a3b33e822728461dd773ea0b5b841043d5c312` 发布到测试服务器，970 个跟踪文件核对一致，服务/权限/回调验收通过；[排查、备份及发布记录](validation/2026-09-12-staging-mcp-auth-fix.md) 保留每一步实际结果。上一候选选择窗口已过期，最终线上 BC 读取需用户重新授权，未伪造授权/延长期限。

## 2026-09-12：MCP 授权后额度与工具定义排查

- 第二次租户授权已兑换并加密保存候选令牌；候选 BC 读取被空 TIKTOK_CALL_POLICIES 拦截。测试环境补齐本地限流：每逻辑操作 3 次/秒、全局 10 次/秒，并发分别 app 4 / endpoint 2 / tenant 4 / advertiser 4，lease 960000ms；全局数字为本地调度策略，不是官方 App 额度。未核实主体时保持所有 MCP 连接共用 shared-unverified 配额域。
- 配置变更前完整备份 `/var/backups/tt-ada-staging/20260912T070256Z/`，含 PostgreSQL、Redis、项目源码/已构建前端及私有配置。PG 新库恢复、Redis 独立实例恢复、项目隔离解压和 SHA-256 均通过，RELEASE_COMPLETE 已写入；API/Worker/Beat 恢复。
- 实际 tools/list 已可请求；7 个账户工具中发现空 properties 省略及 number 的 double 注解差异。仅规范化这两类表示，保留字段、类型、边界、字符串/未知格式及业务字面量校验。新增 6 个回归先失败，再修复后完整协议测试 50 passed；Ruff、ty、diff 检查通过，真实 7 个脱敏工具定义离线比较通过。
- 当前修复待固定版本发布及候选 BC 读取复验；未替用户绑定 BC、设默认连接、上传或创建广告。

## 2026-09-12：发布规范与双通道状态核对

- 补充 AGENTS.md、通用及测试环境部署手册：每次实际发版均备份数据库、Redis、独立项目源码/构建产物归档和私有配置，核对配置并验证恢复；无迁移也适用。明确现有测试服务器备份脚本尚无项目归档，后续发版需人工补齐，未改写历史备份事实。
- 核对 accounts/routing.py：明确连接优先，否则 BC 唯一默认连接；未设置则阻断。授权不自动改默认，冻结任务不随默认切换或跨通道重试。
- MCP 实际证据仍为官方登记 HTTP 201 与本地 configuration READY；尚未完成租户授权和真实账户/上传/广告请求。文档修订不改变运行代码或服务器配置。
- 验证：git diff --check、变更 Markdown 本地链接检查；未重复运行业务回归。本次提交主题为 docs: require complete backups for every deployment。

## 2026-09-12 测试服务器发布准备与 Linux 验证

用户明确授权提交、推送全部本地代码并部署新加坡测试服务器。历史建设方案已纳入 `46db70e`，此前 33 个本地提交已推送到现有 `feat/platform-implementation`；本地及远端均无 `main`，未创建分支或强推。

- 767 文件与此前全量矩阵快照核对一致后，只修改三个测试文件；业务源码未变。服务器冻结安装成功，使用 Bun 执行 `tsc -p tsconfig.build.json` 和 Vite 构建通过，Ruff 和显式指定虚拟环境的 ty 检查通过。
- Linux 专用验收共 9 个不同用例通过：源素材双通道/迟到及硬终止 4 passed（83.22 秒）；广告原连接硬终止恢复 2 passed（81.06 秒）；同轮已通过广告硬期限及原件校验 3 项。真实 PostgreSQL 18/Redis 8、隔离测试 DB14/15；不调用真实 TikTok/R2。
- 修复测试 Redis 库错配、两个嵌入 worker 共用 Kombu Hub/停止标志、假 HTTP 服务一秒空闲连接复用竞争、模拟回读金额表示，以及恢复测试过早软中断/硬终止。源素材真实硬限仍为 3 秒；广告恢复为 10 秒，仍断言实际到达后进程硬终止、只创建一次、原连接读取和撤权阻断。生产任务期限不变。
- TikTok 官方动态客户端登记实际返回 HTTP 201；测试站点独立注册材料已保存于服务器受控文件并通过应用用户读取校验，回调为 `https://tk-ada.137-220-150-31.sslip.io/api/integrations/tiktok/mcp/callback`。注册与租户登录授权分开；未使用 Codex token，未完成租户 TikTok 授权或广告/素材真实联调。
- 既有数据的独立恢复副本已成功演练迁移至 `mcp_cover_evidence`。服务器增加 2 GiB swap；备份脚本覆盖 MCP 注册配置。实际库迁移、服务切换、备份恢复、双管理员登录与 MCP READY 均已通过，运行提交 `0535f050dab73632a2ea4f4bb801dca0871de238`；见 [发布验收](validation/2026-09-12-staging-mcp-release.md)。

# 实施进度

## 2026-09-12：MCP P3.6 双通道集成与离线验收

- 官方 API 与无 API App 配置的 MCP 两条合成链路，经过实际场景任务、原件验证与入库、来源上传、跨账户目标分发/封面、具体预览提交、独立 CTA、三级直接 ENABLE 创建及实际父子/金额/素材回读。业务使用后端代码中的官方客户端；租户授权、BC 绑定和冻结连接规则贯穿各步骤。
- 完整首跑为 2149 项通过、36 项失败、8 项初始化错误、9 项跳过，保留原日志。修复发送前期限错误丢失 NOT_SENT 类型、既有已验证封面的复用顺序，并同步旧测试的授权事实、历史列及逻辑操作限额配置；各修复经独立审查与定向验证，没有放宽业务门禁。
- 最终完整六目录后端矩阵 **2203 项通过、0 失败、9 项跳过，1304.32 秒（21 分 44 秒）**。其中 8 项真实 Linux prefork 验收在 macOS 未运行；另 1 项为 API 分支不适用的 MCP 文本 envelope 用例。前端 workspace **348 项通过（3.9 分钟）**，TypeScript/Vite 构建通过；完整 app ty、Ruff app/tests 通过。阶段测试不与完整矩阵累加计数。
- P2.4 已提交 `2429a22`，单一迁移 head 为 `mcp_cover_evidence`。P3.6 已提交 `10da9fd`，覆盖整合类型检查、回归、历史迁移与发布/恢复记录；完整代码提交和后置核对登记于 [离线验收](validation/2026-09-11-tiktok-dual-channel-offline.md)。代码与测试完成本地整合，未将 23 项计划等同于全部环境验收。
- 正式 MCP 客户端注册、真实租户授权及主体/权限/工具响应、视频服务策略仍待 [真实联调](acceptance/live-mcp.md) 核实；MCP 视频门禁保持关闭。Linux 恢复验收、真实 TikTok/R2/版权方写入及部署未执行；没有复制 Codex 凭据、推送或发布。子 Agent 与完整测试均自然结束，未因等待超时中断。

## 2026-09-12：MCP P2.4 目标封面与持久回执

- 图片上传、详情和建议封面接入冻结目标连接的 API/MCP 网关，保留官方 API 的封面权限叶规则。每次实际请求检查当前读取/上传权限、目标 VID、原视频 MD5 和领取状态；仅采用本目标视频详情或同比例建议，不自动裁图或绑定其他素材。
- 新封面任务保存不可变原视频 MD5；实际图片 ID/签名在客户端清理前持久化，主事务失败时由独立回执保存。迟到回执与已核实图片冲突时撤下该目标的冲突图片映射、保留 ID 并持续阻断；普通核查不能清除已证实的冲突。
- 状态查询使用原连接当前只读权限，查看者不需要搭建权限；默认切换不代替原授权。历史原摘要或回执观察缺失时保持阻断，不补写今日事实。新增迁移 `mcp_cover_evidence` 经独立审查，专属本地测试库备份、升级和 Alembic check 通过；已有新证据时拒绝降级丢失。
- 完整矩阵发现新摘要校验意外拦截无历史封面任务的既有已验证图片；已恢复其当前连接读取权限与有效期校验后复用。已有任务仍核对原摘要，新封面仍须保存摘要；新增双通道正反例没有给旧图片补造原件证据。相关旧迁移测试按真实旧列播种，避免当前模型字段提前进入历史库。
- 规格/质量最终复审通过；原 11 文件聚合 357 项通过、1 项既有 SDK 不适用例跳过，最后封面增量与广告执行九文件 187 项通过（209.91 秒）。整合后的全 app ty、Ruff app/tests 与相关 mypy 通过；完整后端首跑失败及各组修复保留记录，最终完整矩阵待复跑，不将阶段数量相加当作全量结果。
- P2.5 已提交 `b3f9ada`。本轮以 `materials: verify channel covers and preserve receipt evidence` 本地提交；未访问真实 TikTok 或生产环境，MCP 视频入库真实服务策略仍未核实。

## 2026-09-12：MCP P2.5 未知素材恢复与原件保护

- 修复分多次任务搜索素材时只检查总页数的缺口：现在持久核对总行数、连续页、已见视频 ID 摘要及完整计数。目录变化、重复、历史游标缺证据或超限均保持未知，只继续核查；完整唯一候选仍须按实际 VID 核实摘要和大小。
- 接受上传后失联、签名 URL/本地租约到期、迟到回执、其他操作使用同一原件及完成事务回滚时，原件用途和容量预留均保留。现有清理保护已通过回归，无需额外改写清理逻辑；每轮最多 100 页/10,000 条，持续变化按既有 60 秒间隔只读核查，不自动重传或超时释放。
- 独立规格/质量审查通过；作者八文件 103 项通过，根级两个文件 67 项通过（48.82 秒），相关 mypy、ty、Ruff 通过。新增四项 Linux 双 worker/prefork 故障用例在本机未执行，不能将跳过算作通过。
- 本轮以 `materials: preserve originals across unknown channel uploads` 本地提交；无新增迁移、真实上传或生产变更。封面整合与全矩阵继续执行；真实 MCP 视频上传策略仍待独立服务证据。

## 2026-09-12：MCP P3.3–5 广告执行、只读恢复与连接页面

- 广告创建迁至官方 API/MCP 两个适配器，按具体预览直接 ENABLE，CTA 独立执行。原请求和尝试编号在发送前保存，实际 ID 先提交再关闭客户端；明确未发送可保持原正文和编号重新排队，结果未知不重建或换通道。每次实际请求前检查原路由、当前权限、任务领取和场景/素材证据。
- 普通回读核对实际账户、父级、完整字段和全部分页；新授权只读核查必须管理员显式发起并证明同连接、同主体和同资源，结果独立保存，找到对象也不会继续原广告创建。新增不可变历史核查迁移和后台修复调度，支持按原 request_id 找回丢失回执。
- 草稿可明确选择当前 BC 的 API/MCP 连接，或清除选择使用 BC 默认；只有新准备解析偏好。预览和执行详情展示实际冻结连接，权限不足、历史证据缺失和 UNKNOWN 显示相应可操作状态。独立核查明确结束后可再次显式只读核查；受理未知或运行中仍只查询原请求。
- 独立审查发现并修复回读伪 ID 和页面终态无法再核查的问题，中文广告名称不受影响。公开响应仅给业务字段，不暴露 token、授权回调或原始 MCP 响应。草稿连接迁移与历史核查迁移均经独立审查、本地升级与历史数据保护验证；单一 head 为 `mcp_draft_connection`。
- 验证：执行作者 76 项、适配器 69 项，根级执行/状态/公平 35 项通过；恢复作者 185 项通过、3 项 Linux 专用测试未运行，ID 修复后 67 项通过；根级恢复/路由联合 30 项通过，唯一测试分页假设修正后相关 5 项通过。页面原整组 184 项通过，终态补丁后相关 16 项通过；TypeScript/Vite、相关 mypy、Ruff、Biome 通过。暂存版本独立导出后，另有 15 项跨执行/历史 HTTP/迁移/草稿/展示用例通过（16.78 秒），同版前端类型检查和 Vite 构建通过（438 毫秒），确认不依赖后续未提交改动。测试组有重合，不累加成总数。
- 三项分别通过规格/质量审查，因共享类型、执行恢复接口和连续迁移依赖合并为一次本地提交 `builds: execute and reconcile frozen API and MCP requests`。没有推送、部署、真实 OAuth/TikTok/R2 或广告操作；完整素材封面/清理和跨阶段验收继续执行。
- 已知环境限制：macOS 无 Linux prefork 实测，三个 hard-kill 用例不能以跳过视为通过。先前一次 MCP `mcp_call_failed/NOT_SENT` 未复现且尚无已核实根因，后续相关完整回归通过；未新增重试或调整期限掩盖异常。

## 2026-09-12：MCP P2.3 素材上传与实际回读

- 来源 URL/FILE 入库及跨账户目标分发改用统一 API/MCP 网关，按原任务连接逐次检查权限、账户和领取状态。实际 VID/MID 先保存，再关闭客户端；结果未知只核查原操作，默认切换不补传。
- MCP URL 上传采用按契约版本登记的容量、内容身份及自动行为门禁；真实服务证据仍未核实，生产入口保持关闭。原有 API URL/FILE 路径保留应用容量与明确参数，由实际摘要、大小和 VID 回读决定可用。合成测试能力不能当作真实上传能力。
- 原件 UNKNOWN 用途与字节预留持续保留；当前领取者获得明确未发送结果时，只结束这次临时签名用途。批量同目标权限核查已消除重复查询，单条和 30 条素材均为 34 条只读查询，原规模不缩减。
- 独立规格/质量审查通过；作者最终 477 项通过、1 项仅适用 MCP 的参数例跳过，加强实际凭据轮换后 11 项通过；根级实际 PostgreSQL/Redis 与传输边界 57 项通过（21.92 秒）。暂存版本另在新建独立 PostgreSQL 测试库验证同组 57 项通过（22.10 秒），确认不依赖后续未提交代码。相关 Ruff、mypy、差异检查通过。无新增迁移、真实 TikTok/R2 调用或生产操作。
- P2.2 已提交 `e478511`；本轮以 `materials: upload through frozen API and MCP gateways` 本地提交。封面与完整原件清理验收继续执行。

## 2026-09-12：MCP P2.2 素材父任务与实际来源路由

- 新上传批次、R2 素材接收和各次素材操作在排队前保存连接；目标分发和封面继承搭建父路由，来源读取保存实际上传账户与连接。切换默认连接不会改变已接受的任务，幂等重入读取原记录；普通凭据轮换仍可用，授权或契约变化明确阻断。
- 同一素材 VID 的历史封面身份不会因换连接重复创建，实际上传连接不被目标执行连接覆盖。旧记录缺少原授权证据时保持未知，数据库禁止补写或改写其路由；新增迁移保留原请求、摘要和远端 ID，含路由证据时拒绝降级。
- 独立规格/质量审查通过。作者素材组合 100 项、来源/并发/封面等组合 99 项、搭建调用方 39 项通过，最终封面修复 44 项通过；根级最终 20 项实际 PostgreSQL/Redis 回归通过（4.15 秒），迁移独立审查、升级及 Alembic check 通过。相关 mypy、Ruff 通过。各组覆盖有重合，不累加为总数。
- P1.8 已提交 `9df5b2b`；本轮以 `materials: freeze upload and distribution routes` 本地提交。P2.3–4 继续接入素材写入网关，当前 MCP 素材写入口仍明确关闭；没有真实 TikTok/R2 上传或生产操作。

## 2026-09-12：MCP P1.8 租户连接页面与 HTTP

- 管理员可选择官方 API 或 MCP 授权；MCP 独立于开发者 App 配置，授权后明确选择一个 BC 绑定。账户列表按租户、BC、连接查询，无默认时等待显式选择；管理员可为指定 BC 切换默认。连接详情区分读取、上传、搭建权限的已知与未知状态。
- 授权取消或失败保留已有连接。页面持续更新正在刷新的凭据状态，到终态停止；绑定成功清除回调参数；分页权限被撤销时隐藏已缓存的受保护连接名。重新授权后的当前状态不会被旧刷新异常覆盖。
- 后端与页面分别通过独立规格/质量复审。最终真实本地 PostgreSQL/FastAPI 23 项通过（包括原 10,001 条目录分页），workspace 浏览器 84 项通过；TypeScript/Vite 构建、相关 mypy 与 Ruff 通过。浏览器使用 HTTP 边界合成数据，没有真实授权或广告操作。
- P1.6/P1.7/P3.2 已提交 `3bfce19`；本轮以 `accounts-ui: manage tenant MCP authorization and BC connections` 本地提交，后续素材与广告执行继续实施。

## 2026-09-12：MCP 账户目录、场景与搭建父路由

- P1.6：API/MCP 使用完整分阶段目录后原子发布授权与账户，旧记录在失败时保留。过期事实在原连接上完整重新观察，普通令牌轮换不使任务失效；完整授权集合可收紧同连接其他 BC 的失效授权，缺失详情不会清空另一连接已核实的账户元数据。MCP 写权限没有独立证据时仍保持未知。
- P1.7：草稿准备排队前固定连接，场景分页、账户能力与后续预览都明确校验原路由。MCP 握手和每次业务 HTTP 前重新核实当前权限及任务领取，失效后不再发送；刷新结果未知或需重新授权保留明确阻断原因。旧 SDK 场景第二调用路径已移除。
- P3.2：预览、执行步骤与每次尝试保存原连接、授权及契约版本，默认切换只影响新的准备。迟到回执关联原尝试；历史无法证明原授权的任务保留请求、UUID 和远端 ID，阻止按今日默认续建。历史伴随迁移、场景路由和目录观察语义共用连续迁移链，因此三项已分别审查的依赖一次提交，避免中间版本缺少必填父路由。
- 验证：P1.6 最终 139 项联合回归及两项各 5,000 条容量测试通过；P1.7 作者完整 117 项、共享网关 64 项通过，根级追加四项真实 HTTP 边界测试和修复夹具后的 49 项场景任务、五项草稿回归通过；P3.2 作者 175 项与根级 19 项历史/路由验证通过。相关 mypy、Ruff 通过，规格与质量审查均批准。根级将本次暂存版本独立导出并使用全新 PostgreSQL 测试库，再验 48 项跨模块边界通过（51.34 秒），确认没有依赖后续未提交代码。各组覆盖有重合，不相加为总数。
- 并行验证暴露的旧测试全表读取和重复插入绑定已按合成租户范围修正；没有降低测试数据量，也未因等待时间中断子代理。全部验证使用本地 PostgreSQL/Redis 与传输层替身，尚无真实 TikTok、Linux worker或生产验收。本地提交以 `tiktok: preserve authorization routes across directory and build work` 标识；素材写入和广告 worker迁移继续独立实施。

## 2026-09-12：MCP P3.1 广告类型与双通道回读

- 新增完整冻结创建意图及 API/MCP 只读适配器，覆盖 Campaign、Ad Group、Ad、CTA 和广告组状态。原有字段可完整编解码，保留精确 ID、预算、ROAS、素材、链接、文案及直接 ENABLE 意图；新适配器创建入口仍明确关闭。
- 回读只采用远端实际字段，不从请求补齐。独立审查复现 SDK 将原始小数舍入后错误判定匹配；修复后远端浮点缺乏精确依据时返回证据不足，历史本地输入兼容与远端解析分开，精确整数和字符串仍可比较。
- 整体独立审查及精度修复复审通过。初始合同、HTTP、网关及旧 SDK 组合 96 项通过；最终精度相关 45 项真实客户端 HTTP 与 25 项合同测试通过，mypy、Ruff 和格式通过。网关根级组合 37 项通过；全部为本地替身，没有创建真实广告。
- 已知限制：CTA 回读缺少实际账户字段、Smart+ 广告组需另读状态、普通 JSON 小数金额的精确性，以及未知 CTA ID 检索仍需后续能力/恢复阶段处理。此提交不代表完整写入闭环或线上验收。本地提交以 `builds: add typed API and MCP readback contracts` 标识。

## 2026-09-12：MCP P2.1 素材双通道读取

- 统一视频、图片、封面建议和临时预览的类型与 API/MCP 适配器，通过同一任务网关按冻结来源连接读取。返回前再次核对原授权、绑定及素材的持久标识；正常凭据轮换仍可继续，授权收缩或素材变化立即阻止返回预览。
- 初始来源查询、网络读取及最终校验共享绝对期限。预览读取不投递素材准备或写入任务；凭据不足时沿用统一凭据刷新调度。缺少真实权限或工具能力的上传仍关闭。
- 独立规格/质量审查两项问题已修复并复审通过。材料相关组合 220 项通过、1 项不适用跳过；最终修复相关 29 项及既有预览 2 项通过，另复核 10 项精确错误码与正常轮换。真实 PostgreSQL 锁等待、本地 SDK/MCP HTTP 替身及静态检查通过；未访问真实 TikTok 或上传素材。
- 旧素材 SDK 原子操作暂由新适配器共用，后续 P2.3–4 迁移全部写调用后删除旧入口；未宣称素材 worker 已全部迁移。本地提交以 `materials: read assets through frozen API and MCP routes` 标识。

## 2026-09-12：MCP P1.5 固定 BC 路由与统一网关

- 新任务从 BC 的明确默认连接或显式连接冻结路由；默认改变不会改变已有路由，缺默认时要求管理员选择。管理员设置默认的 HTTP 入口校验租户、BC 与绑定，记录审计。
- 统一工厂创建官方 API/MCP 的账户与场景适配器，逐次实际请求检查当前操作者、连接、账户、授权和契约修订，并使用独立短事务及共享准入。仅 API 分支要求开发者 App；业务任务和冻结路由不携带 token。
- 普通 token 轮换可继续使用原路由；已打开会话期间凭据变化会在下一次发送前停止，调用方可用同一路由重新打开。目录/角色刷新可在账户证据过期时进行，普通账户操作仍要求当前完整证据，401 不自动刷新重放。
- 完整规格和质量独立审查通过。网关、路由与权限 72 项通过；根级默认设置 HTTP、路由与既有列表 32 项通过，相关 mypy、Ruff、格式和语法检查通过。全部为独立 PostgreSQL/Redis 与合成 HTTP，本任务尚未迁移后续素材及广告 worker。本地提交以 `accounts: pin BC routes across background work` 标识。

## 2026-09-12：MCP P1.4 自动刷新与回执恢复

- 网关只按任务期限检查凭据，需要刷新时持久排队；刷新 worker 通过连接锁、一次发送标记和加密回执恢复，正常轮换只增加凭据修订，不改变冻结任务的授权版本。完整响应在连接清理前保存，响应未知不重放旧 refresh token。
- 每个发送和发布边界重查排队操作者的当前租户权限；权限变化保留已收到回执，不越权发布。恢复消息与 publisher 的发布事务通过行锁同步，避免 worker 把自己的原始消息误当成后续恢复消息。
- 独立规格/质量审查及两轮定向复审通过。最终 31 项真实 PostgreSQL/Redis 与合成 HTTP 测试通过，Ruff、格式、mypy 和语法检查通过；没有真实 OAuth、TikTok 或生产调用。
- 同一授权的标准刷新沿用已验证的客户端、服务端与 scope 链路；TikTok 丢响应后的轮换/重放保证仍未验证。范围或身份变化的候选保留加密材料并要求管理员重新授权，未宣称已有直接接受刷新候选的界面。本任务以 `accounts: refresh MCP credentials without replaying unknown requests` 本地提交。

## 2026-09-11：MCP P1.3 租户授权与 BC 选择

- 接入不依赖 Marketing API App 的独立 MCP 授权：固定官方端点、PKCE、持久防重放 claim、加密候选和完整 BC 目录。租户管理员选定 BC 后只排队验证，完整目录发布由 P1.6 处理；未提前激活连接。
- MCP 授权及候选全过程持续检查有效租户成员关系和当前管理权限。完整工具目录中，可选上传/广告工具契约漂移只限制对应能力；账户必需契约仍严格校验。旧页面和新入口共用本地停用、授权版本与候选任务清理。
- OAuth 增加清理前回执钩子供刷新使用，原始客户端标识与令牌一同加密保存；数据库及 Redis 检查使用独立短连接与期限。驱动超时不替代 worker 硬期限，也不声称能绝对取消同步 DNS/网络分区等待。
- 独立规格/质量审查发现两项权限及能力隔离问题，修复复审通过。授权/绑定最终 46 项通过；此前含旧回调回归 42 项、根级路由/资源期限 21 项、客户端绑定定向 1 项通过，相关 mypy、Ruff、语法检查通过。均为本地合成验证。
- 运行时需独立部署注册材料，未借用 Codex token，未执行真实注册/授权或发布。远端撤销因 grant 隔离语义未核实而明确返回不支持；本地停用可用。本任务以 `accounts: authorize tenant MCP connections and BC candidates` 提交。


## 2026-09-11：MCP P1.2 账户与场景读取契约

- 统一 API/MCP 的账户目录、BC 角色和场景事实，保留实际广告账户 ID、明确分页与未知权限；运行连接限定绑定 BC，候选读取使用独立上下文。两个适配器复用同一规范化逻辑，API 继续使用固定官方 SDK。
- 独立审查发现官方 SDK 会把 false、浮点数和字符串业务码转换为整数；在 SDK 原始反序列化入口补上严格校验，避免错误结果被当作成功。每次实际请求仍独立检查准入与期限。
- 初始读取及相关回归 105 项通过；审查修复先复现 6 项失败，修复后双通道 32 项通过，规格与质量复审均通过。相关 ty、Ruff 和语法检查通过；未进行真实 TikTok 调用。
- 本地提交以 `accounts: unify API and MCP read contracts` 标识。后续继续授权、刷新与运行路由接入。

## 2026-09-11：MCP P1.1 租户连接与 BC 模型

- 新增通道类型、凭据/授权/契约独立修订、授权事实、工具观察、MCP 授权候选/刷新 attempt、BC 绑定和默认路由。数据库约束保证租户与连接对应、每条 MCP 连接只绑定一个 BC、默认只指向该 BC 的有效绑定；缺失上游主体/grant 保持空。
- 新迁移 `mcp01` 从实际 `0017_preview_naming` 生成，5 个 TikTok 列原位改名并同步全部调用方、脚本和测试；版权方版本和历史 JSON/远端 ID 保留。既有 API 多 BC 关系全部回填；只有唯一有效连接的 BC 自动设默认，多条有效连接留空。存在 MCP 连接时拒绝降级，防止旧代码把 MCP 凭据当作 API 凭据。
- 规格和质量独立审查通过；相关账户、场景、草稿、来源上传 385 项回归通过，其中含 10 项模型测试和历史升级/降级保护。根代理另验证既有历史迁移/目录 23 项通过；全新专属测试库升级及 Alembic check 通过，模型无未生成差异。改动脚本语法与 Ruff 通过，Alembic env.py 仅保留既有导入顺序例外。
- 测试并行时曾提前执行空迁移骨架，仅影响专属测试库；已废弃该库并建立新测试库完成上述升级与回归，未修改应用或生产数据库。后续迁移在审查和根代理串行升级完成后才恢复并行测试。
- P0.3–4 已提交 `131b5f7`；本任务以 `accounts: model TikTok channels and BC bindings` 本地提交，不推送。继续 P1.2 读取与 P1.3 授权；真实授权和写能力仍未验收。

## 2026-09-11：MCP P0.3–4 会话与共享准入

- 后端直接使用官方 MCP Client，任务各有独立会话和绝对期限；实际 HTTP 请求逐次鉴权/准入，阻止 SDK 隐式重发、重定向和发送后补查目录。收到回执后关闭失败不覆盖结果；发送后无法确认则保持 UNKNOWN 并停用该会话。
- API 仍使用真实 App 配额域；MCP 在未核实独立上游额度时共用保守配额域，连接与候选 ID 不能拆分额度。候选仅能进行协议和账户目录读取，不伪造 BC；正常清理释放并发占用，中断保留到期。
- 独立 HTTP 审查定位并修复 SDK 独立日志、UNKNOWN 并发竞态和主线程中断丢失。51 项本地 HTTP 测试覆盖真实官方客户端、两个确定性竞态和三类中断；协议/解析/传输/通道准入组合 174 项通过，既有及新增准入组合 54 项通过。显式清空三项 API 配置后 MCP 本地调用成功，API 仍拒绝。
- 两项规格与质量复审通过；P0 八份源文件 mypy、传输 Ruff/ty/语法及差异检查通过。复审后仅补类型标注和精确测试键清理，后者定向复测通过。测试使用独立 PostgreSQL/Redis 与本地 HTTP，没有真实 OAuth、TikTok 或广告写入；这不代表真实授权或生产联调通过。
- P0.2 已提交 `8ed44cb`；P0.3–4 因实际 HTTP/Redis 回归相互依赖，以 `mcp: add bounded sessions and shared admission` 一次聚焦提交，避免提交间测试缺依赖。不推送，继续 P1 连接授权。

## 2026-09-11：MCP P0.2 冻结上下文与结果解析

- 新增不含凭据的冻结通道路由、调用证据和副作用错误类型；仅接受契约允许的完整业务回执。自然语言成功、冲突响应、非零业务码、重复 JSON 键和无法确认的回执保持 UNKNOWN，不自动重发。
- 独立规格和质量审查通过；结果解析 68 项，协议/结果组合 107 项通过；相关 Ruff、mypy、语法检查通过。合成结果夹具没有被记作真实 TikTok 返回证据。
- P0.1 已提交 `f88884f`；本任务提交以 `mcp: add frozen routes and strict result decoding` 标识，不推送。传输和配额仍在独立审查。

## 2026-09-11：MCP P0.1 协议与工具契约

- 按用户要求开始分派实施，所有子代理使用 gpt-6-astra/high。固定官方 MCP Python SDK 2.2.0 和 httpx2 2.12.0；公开只读 metadata 核实官方 endpoint、issuer/resource、授权端点与 PKCE，保存公开来源及未核实项。
- 建立账户、场景、素材、广告操作白名单与 schema 比较。工具声明属于文档证据，尚未使用本产品真实授权观察；正常 token 轮换保证、账户写权限和远端大小上限仍未核实。
- 独立审查发现并修复 boolean 枚举、JSON boolean/number 比较及依赖字段语义问题；最终协议 39 项、与结果解析组合 107 项通过，相关 mypy/Ruff/语法及摘要检查通过。没有注册、授权或业务写入。
- 本地提交以 `mcp: define verified protocol and tool contracts` 标识，不推送；后续继续传输、配额与 P1 连接实现。

## 2026-09-11：TikTok MCP 双通道实施计划

- 用户以“没问题”确认书面设计，继续编写实施计划。[总览与执行顺序](superpowers/plans/2026-09-11-tiktok-mcp-implementation.md) 关联 P0 协议、P1 授权连接、P2 素材及 P3 广告四份计划，共 23 个任务；每份列明文件、公共接口、失败用例、实现要点及验收命令。
- 根代理完成设计覆盖和跨阶段接口自查：明确凭据修订与授权语义分离，任务绝对期限沿调用链传递，候选 tools/list 先于业务读取；父预览路由先落库，再一次性更新场景/素材准备函数全部调用方。迁移按实际 head 串行生成，先完整只读，再开放上传与创建。
- 同步设计确认状态和 AGENTS.md 入口。保留现有直接 ENABLE、全部剧目投向全部账户、实际素材来源及 R2 未知用途保护；不引入另一套激活流程，也不复制 Codex 登录凭据。
- 文档验证：5 份计划必需结构与占位符检查、13 个本地链接检查、44 段 Python 示例语法检查通过；语法检查不等于示例执行或业务测试。提交前检查 Git 差异及空白。未安装依赖、修改业务代码、运行应用测试、迁移数据库、注册客户端、真实授权、上传素材、创建广告或部署。
- 本地文档提交以 `docs: plan official TikTok MCP dual-channel implementation` 标识，不推送。下一步从 P0 执行，真实协议、离线测试及目标环境联调分别登记，不能把计划中的预期结果记为已通过。

## 2026-09-11：TikTok 官方 API 与 MCP 双通道书面设计

- 用户确认复用目前骏伯、星屿使用的 TikTok 官方 MCP，后端代码直接调用；连接由各租户管理员自行授权并明确绑定 BC。采用同一业务接口下的 API/MCP 两个适配器，官方 API 保留固定版本 SDK。
- [书面设计](superpowers/specs/2026-09-11-tiktok-dual-channel-mcp-design.md) 已形成，用户随后确认并完成上述阶段计划；未实施代码、数据库迁移或部署。AGENTS.md 同步登记用户变更后的通信约束，旧“仅 SDK”设计作为历史保留。
- 设计包含独立授权/刷新、租户与 BC 隔离、固定父子任务连接、凭据材料与授权语义版本分离、共享限流、R2 用途保护、三级广告与结果未知回读。TK-ADA 继续按具体预览提交后直接 ENABLE；不继承运营 skill 的轮转、固定预算和停用后启用流程。
- 只读代码与脱敏连接配置核对确认两条 MCP 使用同一公开官方 flat 端点；官方介绍和接入目录支持自建客户端及独立 MCP 授权。具体客户端注册、scope 证据、令牌轮换、工具字段和远端重试保证列入 P0 协议核实，不能将已在 Codex 使用视为 TK-ADA 后端联调通过。
- 验证范围：书面设计自查、独立架构审阅、占位符/本地链接检查及 Git 差异空白检查；未运行应用测试，未读取缓存 token、未调用已登录 TikTok MCP、未执行真实授权或广告写入。本地文档提交以 `docs: design official TikTok MCP dual-channel integration` 标识，不推送。

## 2026-09-11：新加坡版权方连接真实读取验收

- 按用户要求检查其新增的两家版权方连接，目标仍为新加坡测试环境，运行版本 `8b59aed`。经应用租户权限校验后分别调用连接 verify，真实登录、应用发现均返回 200/active，无错误。
- 经现有 open_provider_session 的租户隔离路径，对两家各读取剧目第一页，均返回 20 条且还有下一页。仅验证登录/应用发现/剧目读取，未创建推广链接、修改远端业务数据或调用广告写接口。真实应用及连接 ID 不记录于仓库。
- 已比对服务器与本地 connections/session_refresh 文件 SHA-256 一致，Worker/Beat active。自动重登录实现为发现过期后由业务任务驱动登录及重新核对应用，有限重试/冷却；本轮未人为失效真实会话，不能视为真实过期恢复验收。TK-ADA 自身登录有效期运行配置为 8 天，401 后需要用户重新登录。
- 本轮只新增验收记录，无代码改动或部署；文档提交以 `docs: record staging provider connectivity verification` 标识，不推送。

## 2026-09-11：批量视频单文件上限改为 1 GiB

- 用户要求单文件最多 1G，统一按 1 GiB（1024 MiB / 1073741824 字节）实现，包含上限本身。更新浏览器选择/续传校验与提示、后端 R2/URL 默认容量、Compose、环境变量示例和 R2 配置检查脚本；导入与来源 URL 校验直接读取统一设置，删除旧 256 MiB 兜底值。
- R2 分片、原件流式校验、来源 URL 入库及目标 URL 转存仍共用此容量设置；分片大小、并发、暂存空间与旧 FILE SDK 内存保护保持原值。顺手将同文件中 R2 主机名校验的空值判断改为显式 `is not None`，消除 mypy 类型收窄错误，校验语义不变。
- 验证：素材导入、URL 入库、原件校验、SDK 契约共 173 项回归通过（新增越界用例初次误读错误响应层级，修正断言后 3 项边界复测通过）；另有 R2 配置探测、存储及运行配置 32 项通过。测试使用独立 PostgreSQL、Redis 和外部传输替身。前端上传基础与 R2 队列 65 项通过；TypeScript/Vite 构建、Ruff、Biome、mypy 与差异空白检查通过。新增边界测试只验证元数据与选择行为，不声称已完成真实 1 GiB 网络传输。
- 无数据库迁移，未推送或部署。下次发布须将已有环境的 `MATERIAL_URL_MAX_UPLOAD_BYTES` 更新为 `1073741824`，核对每个并发校验任务的临时磁盘余量，并单独记录真实 R2/TikTok 1 GiB 联调。详见 [R2 运行手册](runbooks/r2-video-upload.md) 与 [功能设计](superpowers/specs/2026-09-10-r2-transient-video-upload-design.md)。本次提交以 `fix: raise batch video upload limit to one GiB` 标识。

## 2026-09-11：命名随机号缩短为 4 位

- 用户要求不超过 4 位，并确认纯数字或字母数字混合均可。新批次号改为 4 位 `0-9A-Z` 短码，例如 `A7K2`，共 1,679,616 种组合；沿用数据库全库唯一约束、最多 10 次碰撞重试和耗尽后明确失败，不接受重复号码。
- 编号生成与当前格式校验共享长度和字符集；旧 12 位数字/32 位 UUID 的未完成预览提示修改草稿重建，已冻结和已提交记录不改名。同一预览及其重试仍复用原短码。
- 策略说明及嘉书/网眼三级命名示例同步缩短；金额精度设置与测试保持原样。功能设计已更新，原 12 位实施计划保留为历史记录并注明新规则优先。
- 验证：独立 UTF-8 PostgreSQL 测试实例中，策略、预览、随机号碰撞/并发和历史迁移 88 项通过（6.40 秒）；策略页面 Playwright 36 项通过（14.9 秒）；TypeScript/Vite、Ruff/Biome、mypy、ty、`git diff --check` 通过。没有真实广告或版权方调用。
- 本轮不新增数据库迁移；前次命名功能的 `0017_preview_naming` 仍须随未发布版本执行。仅本地提交，未推送、未部署；提交以 `fix: shorten ad batch codes to four characters` 标识。

## 2026-09-11：版权方默认命名模板与短随机批次号

- 策略新增默认模板 `{provider_pinyin}-{drama_name}-{drama_id}-{random}`，嘉书及无专用规则的版权方使用该模板；支持点击插入版权方拼音、剧名、版权方剧目 ID、随机号和日期。要求保留剧目 ID 与随机号，拒绝非法变量、属性/下标、转换、控制字符与超长名称，不截断归因名。
- 网眼等已有已核验归因基础名的版权方继续优先使用原基础名及特殊后缀。广告组继承 `-g01`、广告继承 `-sp1` 等编号；模板值中的花括号不会再次解析。
- 新预览使用 12 位数字批次号，沿用全库唯一约束 `uq_preview_batch_short`；冲突仅回滚保存点，最多重试 10 次，耗尽明确失败。相同草稿版本、预览恢复与同一批次不同账户复用编号；保留已冻结和已提交广告原名。
- 新增迁移 `0017_preview_naming`（上游 `r2_part_receipts`），仅为 `preview_drama` 增加版权方规范标识和外部剧目 ID 快照列，不改写旧策略 JSON、冻结名称和摘要。旧版仍在 BUILDING 的预览明确失效，页面提示修改草稿重建；历史冻结预览继续读取原记录。
- 前端分别提供默认模板与专用后缀，展示嘉书和网眼三级命名示例；保留 USD 禁用选择与中性黑色主题。同步生成 API 客户端，原总体设计和策略设计已注明新命名设计的优先级。
- 回归发现原批量验收夹具未固定对象存储类型，继承本地 R2 设置后与其模拟的 S3 endpoint 不符，导致 `object_storage_unconfigured`。仅在测试夹具显式设置 S3 类型与区域，未修改业务存储实现或真实配置。
- 验证：策略与搭建模块分组回归 469 项通过（主组 461 项 / 164.09 秒，批量展开 8 项 / 294.54 秒），1 项 Linux 专用 prefork 用例在 macOS 跳过；真实 PostgreSQL 强制撞号、并发碰撞、耗尽后事务继续写入、幂等复用、快照保护及历史迁移均已通过。策略页 36 项、预览页 26 项 Playwright 回归通过；TypeScript/Vite、改动文件 Ruff/Biome、mypy、ty、Alembic check 和差异空白检查通过。外部调用均在传输边界模拟，没有真实投放或版权方写入。
- [功能设计](superpowers/specs/2026-09-11-provider-ad-naming.md) · [实施计划](superpowers/plans/2026-09-11-provider-ad-naming.md)。本轮仅本地实现与提交，未推送、未部署。发布时须先按目标环境手册排空、备份并执行新迁移，不能只更新前端或代码。提交以 `feat: configure provider ad names with unique short batch numbers` 标识。

## 2026-09-11：策略预算币种默认 USD 并禁用选择

- 按用户要求保留预算币种下拉组件及完整选项，新建空白策略默认 USD，禁用鼠标展开与键盘切换；已有策略和历史版本保留原币种，不自动换汇或改写历史数据。
- 默认 USD 不计入用户修改，避免空白表单离开时被误判为未保存。同步原币种选择回归，验证禁用状态、键盘跳过和实际保存请求中的 USD。
- 验证：策略页面 35 项 Playwright 回归通过（18.5 秒）；TypeScript/Vite 构建、2 个改动文件的 Biome 检查与 `git diff --check` 通过。浏览器测试使用 API 边界替身，未调用外部广告接口。
- 无数据库迁移；本轮仅本地修改与提交，未推送、未发布测试或生产环境。提交以 `fix: default strategy currency to USD and disable selection` 标识。

## 2026-09-10：统一 TK-ADA 应用图标

- 使用用户确认的黑白 A/播放符号图标，统一登录页、桌面侧栏和移动导航；保留 18px 品牌名及 11px 副标题。补齐 32px favicon、180px Apple 主屏幕图标、192/512px manifest 图标和开发者平台上传原图。
- 删除未使用的 FastAPI Logo 组件、模板图标和旧品牌样式；不新增离线缓存或服务工作线程。
- TypeScript/Vite 构建、22 项 workspace-shell 浏览器回归、改动组件与 manifest 的 Biome 检查、git diff --check 通过。代码提交 `8b59aed`；已发布新加坡测试环境，线上图标字节/尺寸、manifest、入口边界、管理员登录与 Worker ping 通过，备份 timer 已恢复。服务器编译因内存限制退出 137，改用相同提交的本地已验证产物；未修改生产环境。[发布记录](validation/2026-09-10-staging-singapore-icon-release.md)。

## 2026-09-10：新加坡测试账号与租户初始化

- 按用户授权，经 sslip.io HTTPS 应用 API 修改平台管理员 `admin` 密码，创建 `junbo` 租户及同名租户管理员；密码仅同步服务器与本地私有配置，不写入 Git。
- 实际重新登录验证 `admin` 为平台管理员、`junbo` 为非超级用户；junbo 仅能看到所属租户，成员角色为 tenant_admin，访问平台租户管理返回 403。未配置外部连接或启用素材自动化。
- 私有初始管理员配置和登录凭据已同步，操作后备份成功。记录提交以 `docs: record Singapore tenant administrator setup` 标识，不推送。

## 2026-09-10：按用户要求停用 Sites 转发

- 删除独立 Sites 项目的转发实现和回源环境变量，发布版本 2，源码提交 `5a1e34fccfeb73a4ebf081c151301394938ddd4f`。旧入口所有请求直接返回 410，不再回源或重定向；未删除 sslip.io 域名或修改 TK-ADA 服务器。
- 构建、改动文件 lint、线上 GET 首页/健康路径和 POST 登录返回 410、sslip.io 健康返回 true 均验证通过。Sites 平台项目记录保留，当前工具没有整站删除接口。
- 本仓库文档以 `docs: record retirement of Sites forwarding` 聚焦提交，不推送。此前 Sites 接入条目为历史记录，以本条停用状态为准。

## 2026-09-10：新加坡测试环境 Sites 域名接入

- 按用户要求发布现有 Sites 域名 `https://ytf-server-gateway.defuelscoulter38963.chatgpt.site`，保留其仅所有者访问设置，转发至新加坡 TK-ADA。Sites 独立源码 `148a0a7fce5fd9487366f7006852b5ff5458e29c` 已上传并发布版本 1，原 IP 入口继续有效。
- 服务器新增 sslip.io 主机名的 HTTPS 回源站点及受信任证书，以满足 Workers 不支持裸 IP fetch 的限制；既有每小时证书续期 timer 接管新证书，dry-run 与更新后备份通过。
- 使用自定义 Worker 在框架 URL 规范化之前转发，保留 FastAPI 尾斜杠、状态码、字节流、业务 JWT，剥离 Sites Cookie 和身份头。5 项边界测试、改动文件 lint、构建与实际线上登录/受保护 profile/静态 JS/API 边界验收通过，未开放 Sites 访问权限或执行外部广告操作。
- 详见 [测试环境手册](runbooks/staging-singapore.md)。本仓库文档提交以 `docs: record Sites gateway for Singapore staging` 标识；应用仓库未推送，运行 SHA 未变化。

## 2026-09-10：新加坡测试服务器无 Docker 部署

- 按用户授权在 `137.220.150.31:22211` 安装原生依赖，以固定提交 `995f89569880df330b02314bfff5e1df33256b1a` 部署至 `/opt/tt-ada-staging/releases/`，未安装 Docker、未复制本地或生产业务数据和集成凭据。
- 公网入口 `https://137.220.150.31`，受信任 IP 证书与每小时自动续期已配置；API、2 进程 Linux prefork Worker、唯一 Beat 由独立 systemd 服务管理并开机自启。PostgreSQL、Redis、API 仅本机监听。
- 服务器冻结依赖安装、前端构建、迁移 head、管理员 HTTPS 登录/受保护接口、72 项真实 PostgreSQL/Redis 登录与调度回归、持久化 outbox→Beat→Worker no-op、浏览器登录页与无 console error 均通过。首次回归因应用角色没有 CREATEDB 导致 8 项测试未通过，改用独立测试角色后全部通过，未扩大应用数据库角色权限。
- 每日私有备份已配置且首次执行成功，PostgreSQL 临时库恢复与 Redis RDB 完整性检查通过；IP 证书续期 dry-run 通过。未执行真实 TikTok/R2/版权方调用，素材导入与清理保持关闭。
- [环境手册](runbooks/staging-singapore.md) 和 [验收记录](validation/2026-09-10-staging-singapore-release.md) 记录服务、版本、备份和边界。本轮文档提交以 `docs: record native Singapore staging deployment` 标识；不推送。当前仓库没有本地 `main` 分支，保留既有 `feat/platform-implementation`，不自动创建分支。

## 2026-09-10：品牌更名 TK-ADA 与标题微调

- 按用户要求统一实际页面、浏览器标签、默认配置与 README 的产品名为 `TK-ADA`。左上角共享品牌主标题从14px增至18px，副标题为11px的“广告投放工具”；登录按钮只显示“登录”，移除箭头，保留加载状态。其余页面字号、颜色、卡片和间距不调整。
- 同步既有登录及移动导航测试定位器。TypeScript/Vite生产构建、33个改动TS/TSX文件的Biome检查、22项workspace-shell浏览器回归和本地实际登录页尺寸核对通过。
- 无数据库迁移或业务逻辑变化。生产仍使用原目录、Compose项目、持久卷及备份服务标识，避免名称变更创建新数据环境；发布时只同步私有 PROJECT_NAME，按生产规则备份、换版并只读验收。
- 代码 `d09c070` 已提交、推送并发布；生产只读浏览器18项、两环境bootstrap、实际18px/11px品牌及登录按钮检查通过。发布前备份完成，数据库head和用户/租户/策略数量保持一致，定时备份已恢复。[生产发布记录](validation/2026-09-10-tk-ada-brand-release.md)包含版本、回退入口与验证边界。

## 2026-09-10：骏伯生产发布与基础验收

- 已按用户授权提交并推送现有代码，生产运行 SHA `016217f65a39330b4b715ab043fe866eea87810c`。入口 `https://manjuad.gzjunbo.net:8000/`，HTTP 自动跳转；原站 80/443 和 8 个原有容器保持正常。
- 生产独立 PostgreSQL/Redis，Alembic head 为 `r2_part_receipts`，API、3 个 Linux prefork Worker 与唯一 Beat 同镜像。平台管理员 admin、租户 junbo、租户管理员 junbo 已建立，密码未写入仓库。
- 真实 API 登录/隔离/策略版本测试、19 项完整生产浏览器检查、18 项重启后只读复验、outbox→Beat→Worker no-op、备份及临时库恢复通过。每日备份定时器已启用并实际试跑成功。
- 专用生产 Compose、Nginx、版本入口脚本、备份脚本与 systemd 单元已提交。构建镜像源调整保留锁定版本/hash，受限 GitHub 下载使用同一官方 SDK commit 的已验证 Git 缓存。后续文档提交记录首发事实，不表示运行镜像自动更新。
- [生产部署/更新/数据库规则](runbooks/production-junbo.md) 已登记在 AGENTS.md，[本次发布验收](validation/2026-09-10-production-release.md) 记录具体版本、证据与限制。相关实现提交：`7c11176`、`f39292d`、`5e1ecda`、`016217f`。
- TikTok API 出站探测两次连接超时，需在真实授权前处理；App、BC、R2 和版权方仍未配置，未执行真实上传或广告操作。该限制与基础上线通过分别记录。以下原有“未推送/未部署”描述是当时阶段记录，不代表当前状态。

## 2026-09-10：产品统一命名为 TT ADA

- 用户要求产品改名为 `TT ADA`，所有实际页面不再使用“短剧投放”“TikTok 工作台”或“工作台”作为名称。已覆盖登录页及页脚、侧栏品牌及移动导航、所有已有路由标题、设置页、根路由兜底和返回首页提示；功能页继续使用素材库、广告搭建等业务名称。
- 同步 README、`.env.example` 与 Compose 默认产品名，并更新既有浏览器测试的文案定位器。技术包名、仓库路径及历史设计记录保留。
- 验证：`bun run build`、改动 TS/TSX 的 Biome 检查及 `git diff --check` 通过；`bunx playwright test --project workspace --workers 4 --reporter line` 全部 **318 项通过（2.1 分钟）**。独立静态复核未发现业务改动或实际页面旧名称遗漏。
- 本地 `http://127.0.0.1:8011` 已更新，私有配置仅同步 `PROJECT_NAME` 并重启 API；OpenAPI 标题为 `TT ADA`，bootstrap 检查通过。使用实际本地登录只读检查登录、平台管理、账号设置、租户功能及 404 共 12 个页面，标题与页面文案通过；截图和检查结果保存在忽略的 `.runtime/tt-ada-*`，不提交凭据或本地数据。
- 本轮提交以 `ui: rename product to TT ADA across all pages` 为标识；未推送。

## 2026-09-10：仓库协作约束更新

- 按用户要求更新 `AGENTS.md`：明确独立仓库、聚焦提交、已有分支工作树、合并与显式推送规则，替换旧的功能分支开发及推送约束。
- 补充中文业务注释、复用封装、旧代码清理、禁止旧业务逻辑兼容/回退、影响评估、统一 shadcn/ui 设计、设计与计划拆分及生产发布要求。
- 验证：检查文档差异、发布手册路径和 `git diff --check`；仅文档修改，无需运行应用测试。提交以 `docs: update repository contribution constraints` 为标识，可通过 Git 历史定位；未推送，未改动已有未跟踪目录。

## 当前状态

2026-09-10 全工作区视觉统一：用户认可策略试版并要求主按钮恢复原黑色、推广全部页面。共享正文22px标题/灰底白卡/16与24px内外距、管理页、素材上传、策略编辑、广告搭建/任务与设置辅助页均已统一，整合源码`27ea0aa`，回归提交`47b074a`。完整workspace **318项通过（2.0分钟）**，TypeScript/生产构建及独立审查通过；实际8011应用已更新并核对。已推送回退标签`ui-before-workspace-rollout-20260910`（`601c181`），主目录与推广分支同步。[实施计划](superpowers/plans/2026-09-10-workspace-visual-rollout.md)及[验收记录](validation/2026-09-10-workspace-visual-rollout.md)记录覆盖范围与联调边界。以下视觉记录保留为历史。

2026-09-10 策略列表视觉试版：用户要求先保留代码再试改，已推送回退标签 `ui-before-refinement-20260910`（`4c732a2`）。本轮仅策略列表恢复22px正文标题、灰底白卡分组、16/24px内外距、固定短列与蓝色主按钮；其他页保留上一版。工作区/租户账户93项、策略36项及生产构建通过，独立评审发现的列宽和长数覆盖问题已修复。实际8011页面截图与跨页恢复已核对；[变更与验收记录](validation/2026-09-10-strategy-ui-refinement.md)保留细节及回退方式，等待用户评价试版阅读体验。

2026-09-10 视觉调整：用户确认后已按官方 dashboard-01 统一中性色主题、288px inset侧栏、16px顶栏标题、自适应内容区及单边框列表，代码 `eb613a4`。原有310项workspace回归、新增5项几何回归及生产构建通过；实际8011应用已更新并核对标题、24px边距与菜单跳转。[实施计划](superpowers/plans/2026-09-10-shadcn-official-alignment.md)与[验收记录](validation/2026-09-10-official-ui.md)覆盖本轮变更，以下保留业务功能交付历史。

2026-09-10：普通账号密码登录、邮箱移除和租户版权方自动续登已交付；运行时使用独立Python协议适配器，不调用网眼/嘉书CLI或共享其账号文件。账号身份保留迁移、自动续登和本地浏览器证据见[账号与续登验收](validation/2026-09-10-account-session-integration.md)，该基线 `a176fd8` 的 [CI 34385153117](https://github.com/yaotingfeng/tiktok-ad-automation/actions/runs/34385153117) 七组通过。

本轮继续实现[独立R2上传计划](superpowers/plans/2026-09-10-r2-batch-video-upload-plan.md)：Task1～7代码已形成，Task8已具备离线浏览器、完整业务链、10k/20k元数据和1,000文件故障验收；最终集成回归与本地更新记录在下节。真实R2/TikTok、CORS、当前媒体域名、生产prefork和日吞吐仍按[运行手册](runbooks/r2-video-upload.md)验收，新导入、自动清理默认均关闭。

| 范围 | 当前行为与证据 | 主要提交 |
| --- | --- | --- |
| 临时对象和预算 | 对象代次不可变；tenant/BC隔离；全局8GiB、租户2GiB为应用暂存窗口，预留和已存不重复相加；清理证据释放一次 | 83adf1e, 3beba10, 0b2cdc1 |
| 导入API与恢复 | 最大20k文件；≤200一批，≤100明细分页；稳定请求回执、发送前持久意图、响应丢失只读回查 | d521929, 7bc1912, eacc3a0, 8642e78 |
| 浏览器批量上传 | 4文件×2分片有界传输；IndexedDB保存恢复元数据；暂停、重选核对、跨范围停止；上传不绑定剧目 | 7582056, 9696fb5, 5dc4135 |
| 分片权限及取消 | 每次权限独立身份；completed实际回查、unused受控浏览器声明、unknown保留；实际签名期限不越过台账；取消竞态不能继续Complete | 301a46f, 2d199cb |
| 校验与来源入库 | 流式MD5/SHA256与ffprobe；事务outbox交接；合法来源公平分流；官方SDK URL上传和强回读，记录实际广告账户 | 0b2cdc1, b745e2e, 2df5e85, 2a440ba |
| 删除后素材使用 | 即时授权TikTok URL接力、新目标实际VID/封面、只读预览；新代次不退回文件上传 | e005bd1, 02c0ee8 |
| 自动清理与回收 | 源强回读成功后即安排删除；exact代次Delete/HEAD、Abort/ListParts/HEAD；消费者或结果未知保留预算；定时100项扫描与审计去重 | 8186adc, bc1c9ba, 76cd455, 856b83f, ce0e12f |
| 部署与验收 | 默认零网络检查；显式probe仅自建key；真实API浏览器3文件/4PUT、6条完整业务链、1,000文件400次清理；独立审查通过 | 20d8fed, dc3f5c7, 304a231, 865158c |

本轮证据按边界区分：[10k/20k元数据](acceptance/r2-ingest.md)、[浏览器→真实API](acceptance/r2-browser.md)、[API→校验→源→清理→目标](acceptance/r2-pipeline.md)。浏览器完整上传3个文件、16,782,295字节，终点是stored及3个待发校验任务；后端6条完整链使用真实数据库/校验/业务逻辑和外部传输替身，不冒称Cloudflare或TikTok实际成功。1,000文件混合故障中400个对象实际经清理worker回收，预算归零；它不是1,000次视频平台入库。

20,000文件选择测量：准备好FileList后16.4ms出现可用界面，DOM100行；合成FileList构造另用1,568.1ms，不能称整个选择过程小于1秒。JS堆为离散样本，未测真实峰值、Worker RSS、慢机P95或生产日量。

### 本轮最终集成验证

以下为合并工作树的实跑证据，外部服务均为传输替身；不沿用账号基线的通过数字。

- 完整后端模块（不含独立acceptance目录）：**1,687 passed、1 skipped，730.06秒**。唯一skip为已有Linux prefork测试；该次启动于 `92cf783`，随后SQLModel类型修复另有34项回归；没有把中途改动冒称已被同一次旧进程载入。
- 最终 `742f94a` 的源选择/源上传、分片权限回归、新validator进程测试、R2 probe/capacity以及6条全链/1,000故障批次联合运行：**60 passed、1 skipped，41.10秒**。skip为新增Linux实际硬终止测试，本机只运行其PG用途归属companion；实际Linux结果由CI提供。
- 前端TypeScript和生产构建通过；完整workspace **310 passed，1.8分钟**。首轮309通过/1失败为退出登录导航时读取旧JS上下文；`d29c699` 等待实际登录导航，保留全部停止传输/不再Complete/清除登录断言，连续5次通过后重跑上述全套。
- 合并分支真实浏览器→FastAPI/JWT→loopback HTTPS R2边界：**1 passed，38.8秒**（场景10.1秒），3文件/4PUT/16,782,295字节，3个stored和3个待发校验outbox，刷新零重传，哈希一致。该测试没有启动素材worker。
- 全量Ruff、Ty无诊断通过，Alembic check无新差异。Compose缺失的11个新R2环境字段已在 `153e704` 补齐，独立CLI实际渲染默认/覆盖×本地/预发布四种配置，API/Worker/Beat/prestart全部一致；未启动容器。
- 代码提交 `153e7045eb1e25fd28a9f9bf0bc75b53aee76118` 的 [CI 34429426415](https://github.com/yaotingfeng/tiktok-ad-automation/actions/runs/34429426415) 包含7个独立任务和新增实际API浏览器验收。链接对应确切代码版本，结果以该运行记录为准；文档追加不替代代码验证。

集成中修复了校验初次投递继承未来时间、签名实际期限超台账、取消与Complete竞态、放弃扫描重复告警，以及离线任务调度器缺失真实prefork上下文等问题。Linux validator测试使用原生产handler/guard和真实独占PG/Redis，3秒测试硬期限杀死子进程后检查旧用途保留；它不证明源上传UNKNOWN全链故障或进程被杀后的临时磁盘回收。

### 主目录与本地应用更新

代码已快进到实际目录 `projects/tiktok-ad-automation/`，主目录和隔离开发分支的代码提交同为 `153e704`。原15份路径整理文档先单独提交为 `aedeacf` 再合并，未覆盖；原有未跟踪 `docs/design-history/` 仍保留。后续文档提交继续同步两个分支。

对实际开发库做私有备份，先恢复到新建独占 `_test` 库，执行三次新迁移和Alembic check，核对用户/密码哈希/租户/成员/策略/已有连接与素材记录保持一致。之后仅停止已登记且工作目录匹配的API、Worker、单Beat，再做最终备份；实际库升级至 `r2_part_receipts`，原记录再次核对一致。测试恢复库已删除，业务数据库和Redis保留。

从主目录重建前端并重启 `http://127.0.0.1:8011`；健康、静态登录、回调业务错误和未知API边界检查通过。用户可见浏览器确认已有admin会话可读用户管理，新增用户表单无邮箱；进入原租户后，原策略v2仍显示USD120、ROAS1.08、10素材/组、3创意，版权方独立凭据表单及自动恢复提示可用。未创建测试业务数据、未验证版权方凭据、未请求TikTok授权或写广告。

本地配置选择R2，两个部署开关保持false，尚未提供实际桶/凭据或TikTok BC。macOS Worker仍为solo基础诊断，素材生产任务需要Linux prefork。因此当前本地页面可访问、数据已保留，不代表本机可以完成真实视频入库与投放。实际部署条件继续按运行手册处理。

### 2026-09-09交付基线（历史）

2026-09-09：用户指定仓库 `feat/platform-implementation` 的 P01～P06 功能与页面已完成；P07 本地业务、浏览器、容量、故障与备份恢复验收已完成。最终代码包括网眼/嘉书持久取链、共享 Scene 准备、目标封面准备和发送前检查、大批汇总与恢复计数优化。没有执行真实广告操作。服务器、域名、App 与租户授权后的外部验收仍按单独清单推进。

最新测试与截图见[离线验收](acceptance/offline.md)、[功能页面对照](acceptance/functional-delivery.md)及[容量记录](acceptance/capacity.md)。下表及后续按阶段保留历史提交，不能用历史计数替代最新提交的验证。

用户实际使用后补充验收：代码已迁入当前工作区的 `tiktok-ad-automation/`；修正平台菜单点击返回原页，以及已在租户内却提示“尚未接入租户”的问题。`35a033b` 的完整工作区回归 260 项通过，`813e119` 补充空白新建策略回归；实际本地浏览器已完成策略创建、编辑与刷新回读。目录迁移、页面验证和运行限制见[首次使用验收](validation/local-usability.md)。这次反馈说明原自动化验收对首次使用路径的覆盖不足，不应把“本地模块通过”表述为“当前环境可以正常完成真实投放”。

| 范围 | 状态与证据 | 主要集成提交 |
| --- | --- | --- |
| P01 Task1 基线与官方 SDK | 冻结安装/构建通过；11 个离线契约，独立审查 PASS | 10ee3ee, dc4d3b0 |
| P01 Tasks2/3 公共契约与进程配置 | API、安全错误/日志、真实数据库隔离、JSON任务、控制队列；独立审查 PASS | 6221f52, 6a66c24, ea18b79 |
| P01 Tasks4/7 可靠投递与共享准入 | 52 个真实 PostgreSQL/Redis 测试；公平轮次、崩溃重投、并发锁、六项原子配额，独立审查 PASS | a01b13a, 34e151c |
| P01 Task5 登录、工作台、回调入口 | API客户端再生成；16 个工作台浏览器测试；构建/TS通过；1024断点与焦点修复独立复核 PASS | 1f75633, 3097c72, 192eeab, b619f9a, 6b5cb77 |
| P01 日志审查修复 | 删除会采集 OAuth 查询串的继承 Sentry 初始化；独立回归与复审 PASS | 8ef97a8 |
| P01 Task6 运行与部署包 | 实际本地管理员登录、静态页面/健康/回调检查、Beat→Worker no-op均通过；Compose两套配置通过；GitHub CI镜像构建通过；没有公网环境 | f192a30 |
| P02 Tasks1/2 租户模型、管理与审计 | 34 项租户回归独立审查 PASS；初始管理员搜索支持完整 ID | e46930c, 05ad598, 183ab6c, 78274e7 |
| P02 Task3 OAuth、加密、回调 | 一次性 state、候选凭据、失败保留旧连接、子进程硬截止；62 项专项独立审查 PASS；实际 callback 已接通 | 7eb5170, 318dadb |
| P02 Task4 完整账户目录发现 | 41 项独立测试及迁移 roundtrip/check PASS；逐页持久化、失败恢复、完成后才撤销旧关系 | be5e44b, aa8ada2 |
| P02 Tasks5/6 权限、目录、批量解析 | 38 项独立回归 PASS，含实际 PostgreSQL 10,001 账户无重无漏分页；配置与停用接口通过 | 0e7b20f, 5ea7018, 4b7f1bc |
| P02 Task7 租户、成员、账户、连接 UI | 全部页面已接实际 API；目录分页、完整字符串 BC ID、登录恢复、未保存表单保护及发现完成刷新已修复，最终独立21项+2个请求边界探针 PASS | 0367ead, 9495a1b, 4be7820, 6052bbe, 0177a69 |
| P03 Tasks1/2 模型与版权方协议 | 87项独立复核 PASS；租户应用与链接身份、嘉书配置对账、网眼公开前端协议证据；没有真实业务联调 | e46ec86, 509397e, d30a8d3 |
| P03 Tasks3/4 取链恢复与 API | 154项协议/工作流测试、381项相关跨模块测试通过；205行队列积压饥饿已复现并修复，最终独立50项复核 PASS | 1d5bcdb, 16322eb, 405937e |
| P04 Task1 目录与映射 | 26项独立复核 PASS；字面匹配、稳定分页、实际素材账户映射、Unicode空VID约束与迁移修复 | 5ac7b9e, 993cdf7 |
| P03 Task5 版权方工作区 | 19 项版权方浏览器测试及实际本地 API 空态通过；应用/历史/候选/安全配置差异；新增后端目录独立3项 PASS | 8c09e40, be9a497 |
| P04 Tasks2/3 原件与SDK上传 | 独立复核 PASS；修复真实PG发送前/回读落库锁序死锁及首次撤权无法恢复；未知结果不直接重传 | b50282f, fd26d5d, 49c793d, 755f7f4, 62129be, e1e8dc1 |
| P04 Task4 就绪与分发 | 独立99项相关测试 PASS；本地只读、共享唯一操作、目标实际VID、撤权后同消息只读恢复；60秒控制队列修复已注册 | 8581955, 5125ba0, 415a878 |
| P05 Tasks1–3 策略基础 | 45 项及额外并发/迁移独立复核 PASS；重复保存准确返回原版本 | 6d583fd, 60b106c |
| P05 Task4 草稿与API | 24 项通过；全行反馈、分批解析、字面素材匹配、可追溯编辑、只读分页；已修复独立审查3类P2，待最终复核 | 415a878, 24448d5, e0b3d37, 3d369d7 |
| P05 策略页面 | 26 项策略浏览器测试，连同其他工作区共128项通过；保存/冲突/回查/租户切换及三种屏宽 | 9d60a15 |

集成 `d6684c1` 的后端模块回归（不含单独运行的 `tests/acceptance`）为 **1,224 passed、1 skipped**，211.72 秒。跳过项为 macOS 不支持的真实 prefork 测试；该测试已在 Linux CI 实证通过。P06 UI 的生产静态工作区运行 **239 passed**，一个仅在开发服务器提供的输入 fixture 测试另行通过；不能把这两次运行合称一次 240 项全绿。SDK/接口替身不代表真实平台联调通过。

- `1660e86` 的 [Platform CI](https://github.com/yaotingfeng/tiktok-ad-automation/actions/runs/34297735517) 后端、前端和镜像三个任务全部成功。`d6684c1` 的后续 CI 曾发现测试调度器缺少 Beat 推进、合法任务数量超过单轮测试上限等问题，已补入实际控制任务和有界分块；当前精确提交状态见顶部离线验收链接。
- `d6684c1` 的嘉书完整验收成功：真实模块准备、Scene 获取、冻结、提交、138 次目标视频上传与回读、138 次目标封面上传与回读，最终 6 Campaign / 18 Ad Group / 36 Ad，全部 ENABLE，预算 600 USD。外部传输为严格替身，业务模块与 PostgreSQL、Redis、outbox 使用真实实现。
- 100,000 账户目录与能力准备、200 剧 × 1,000 目标账户的完整冻结计划及 10,200,000 执行步骤已实测完成；原汇总查询缺陷及备份副本接续验证过程保留在容量报告中。
- 最新 `0014_recovery_candidates` 数据库已重新完成真实 `pg_dump` / `pg_restore` 演练；合成凭据解密、对象元数据与待发任务保持一致，没有启动消费者。

## 远端与新增验证

- `be9a497` 的 [GitHub Platform CI](https://github.com/yaotingfeng/tiktok-ad-automation/actions/runs/34280164998) 后端、前端及容器镜像构建全部成功。该结果对应这个提交，不自动代表后续提交已过 CI；后续提交继续推送并核对精确 SHA。
- P01 UI独立复审补跑900/1023/1024px三项，通过；P01 Task6部署包独立审查通过。遗留模板部署指南已改为当前手册入口，旧Compose移除。
- 本地实际 API/数据库的浏览器流程已走通管理员登录、新建租户、进入租户工作台、读取初始管理员与角色。没有接入真实 BC，也没有创建广告。
- P02 Tasks1/2、Task3、Task4、Tasks5/6 独立审查全部 PASS。界面初次独立审查发现候选搜索冒泡误提交和长表表头滚走；已用 8 项先失败后通过的回归修复，并由 root 执行全部 42 项页面回归。
- TikTok SDK同步响应实际是已校验的`{data,request_id}`字典，计划示例中的统一`.to_dict()`假设不成立；OAuth实现已用实际SDK传输测试发现并更正，后续适配器复用同一解析契约。
- P03独立负载审查复现205行健康批次1800秒仿真后只完成74行，原因是恢复任务不断替换尚未发布的revision。`405937e`保持当前delivery ID/revision，未发布记录保留时间与退避、已发布丢失记录重新待发；修复后600秒仿真全部完成。没有改变外部写结果未知时先回查的规则。
- P05验证见[策略](validation/p05-strategies.md)与[草稿](validation/p05-drafts.md)；P04见[原件上传](validation/p04-object-uploads.md)与[SDK素材合约](integrations/tiktok-materials-contract.md)。

## 执行安排

- 所有执行与审查 sub agent 使用用户指定 `gpt-6-astra`、`high`；不会因耗时长而打断。
- 当前会话主线程持续负责集成、生成客户端、迁移、验证与提交。独立任务使用隔离 worktree、明确文件所有权；共享文件由主线程整合。
- 执行七份阶段计划，对应五批交付。P02完成后并行P03版权方、P04素材、P05策略逻辑；P06只读场景契约先于预览资源集成；再完成广告执行恢复与P07跨模块验收。
- 阶段提交推送到用户 origin 的功能分支；不强推。原资料目录不修改，不复制运营凭据。

## 已确认的实现裁定

- 用户仓库为空，导入固定官方模板快照并保留许可证，origin保持用户仓库。
- 实际模板为根目录 workspace/锁文件，计划中的旧锁路径按真实结构更正。
- 素材上传不匹配剧目；搭建时按完整剧名包含匹配文件名。所有剧目铺同一批全部账户，SP为相同素材不同文案的N条创意。
- 预览提交后三级广告创建直接ENABLE；实现阶段不发真实广告请求。
- 本地P01诊断Worker使用macOS solo验证消息链路，不能把它当作后续任务硬截止证据；真实调用需硬截止小于共享租约有效期。
- App、对象存储和配额未配置时相关业务明确阻止使用，登录/基本租户管理仍能运行。
- 官方SDK视频multipart在内部整读文件；原文件入库可以分片传输，SDK上传另设明确的工程容量上限，默认256MiB及1个同时上传。它不是TikTok官方文件限额，超过时保留原文件并说明阻塞原因。
- 已交付迁移保持不可变；P05先交付`0005_strategies`，草稿/预览后续追加迁移，不回头改已提交的策略迁移。

## 待外部条件

没有实际部署主机/域名与 TikTok App，公网 HTTPS 回调、真实 OAuth、官方SDK真实账户发现和广告试投尚未验证。没有以示例 URL、账户或测试替身冒充这些结果。缺少这些条件不阻止后续本地模块实现。详见 [部署手册](runbooks/bootstrap-deployment.md)。

## 下一步

本地工程与部署包已形成，后续按真实联调清单接入部署环境和租户授权。发布时使用对应提交通过的 CI 与同版本镜像，外部试投和实际消耗分别记录。以下条目保留各阶段当时的验证记录，以本页顶部当前状态为准。

### P02 Task 4: directory discovery

- Schema contract `596c1ae`: six directory models, tenant composite keys, one active connection generation, external ownership constraints; migration `02c_directory` after `02b_connections`.
- Added page-atomic resumable discovery, candidate promotion after complete BC/account enumeration, per-call admission, durable recovery/outbox, and the bounded prefork resource task. Unknown capabilities and incomplete metadata remain blocked.
- Verification: 189 account/tenant/jobs tests passed, including 41 Task 4 cases; independent 41-test review and migration roundtrip passed. Root added pytest importlib mode (`0befa24`) to resolve duplicate test filenames in the standard command. All TikTok transport was fake; full evidence and P07 boundaries are recorded in [discovery validation](validation/p02-discovery.md).
- `sent_count` is a durable attempted-send counter after admission, not proof the HTTP transport started if the process crashed immediately afterward. Recovery relies on persisted page/claim/version state, not this counter.

### P04 workspace material read APIs

- Added tenant/BC upload-request recovery, lightweight upload batch pagination and on-demand 300-second private original preview URLs. GET routes do not enqueue work; viewers can read, and missing original/configuration/scope are explicit errors.
- Batch file counts use bounded parent-page SQL aggregation; original preview uses local SigV4 GET signing with inline allowlisted video MIME and no-store. No bucket permission change or external SDK write.
- Verification: 9 new regressions plus affected upload/progress/retry tests total 58 passed; Ruff, strict mypy and ty passed. See [material read API contract](validation/p04-material-read-apis.md). Root generates the client and independently reviews integration; no frontend files changed here.

## 2026-09-09：冻结预览与素材工作区集成

- 素材只读补充接口独立审查 PASS（64 项）；素材 UI 已合并，工作区 157 项浏览器测试通过，最后补充权限场景后素材完整 31 项通过。
- Scene/OAuth 独立审查 PASS（118 项）；发现的单页/跨页重复 ID 完整性问题已关闭。官方 SDK 创建编译器 26 条离线契约验证通过。
- P05 草稿独立复核 PASS（25 项）；冻结预览实现与 HTTP 接口已就绪，builds 模块合计 124 项通过。冻结预览独立审查、全工作区 UI 独立审查进行中。
- BC 账户权限引导任务正在开发，用完整只读能力检查把 UNKNOWN 授权事实转为可验证的创建/上传权限，避免依赖广告链接或逐剧重复扫描 BC。
- 后续继续搭建工作区 UI、提交执行、结果核查/恢复及 P07 验证。未进行真实广告创建或真实外部业务联调。

### P06 link-free BC capability bootstrap

- Added isolated capability jobs/request aliases/pages/account-role evidence with migration `0005e_account_capabilities` (schema commit `2c107db`).
- Explicit tenant/BC/connection refresh command, read-only status/evidence APIs, 50-row official current-token BC reads, complete-list publication in 100-grant transactions, durable claim/dispatch recovery. Details and integration registration contract: `docs/validation/p06-account-capabilities.md`.
- Dedicated engineering age setting defaults to four hours from first observation; no per-link BC scan and no capability inferred from account visibility. Root owns Scene/draft/preview integration.

## 2026-09-09：搭建执行与任务查询

- 冻结预览独立审查通过；批量输入、原输入完整分页、分组编辑回执、按剧聚合、预算守恒、预览提交与未知响应回查均已接实际 API，UI 集成 `c29710a`。
- 提交与分批展开 `7761232`：同一预览永久只提交一次，同一草稿的历史组合预留不重复创建；账户×剧目展开及步骤持久化均有界。GET 不触发 SDK 或新任务。
- BC 能力目录版本 `ac9bc53` / `a97a21b`：真实 PostgreSQL 验证 5,000 账户后，能力读取按目录版本主键查询，不再逐次扫描整个 BC；目录插入、移动、删除与并发事务均有版本围栏。
- 执行状态、准入与 SDK 创建 `315fe06` / `b1cfde8` / `a1b8764`：请求意图提交后才调用 SDK，已知 ID 及时持久化；Campaign/Ad Group/Ad 直接 ENABLE，SP 共用目标 VID/封面且文案不同。
- 独立审查修复 `7689b26`：CTA 放在官方 `ad_configuration.call_to_action_id`；已收到远端 ID 但 PostgreSQL commit 失败时，在新的安全事务中保留 `LATE_CREATED` 证据；Redis 暂不可用保留未发送步骤重排。文案 100 字符是明确的应用策略。
- 官方 SDK 回读 `771933a`：分页完整性、同名歧义、实际父级与目标素材、CTA、预算、ROAS、链接、文案与 ENABLE 状态均核对；找不到或无法唯一匹配保持 UNKNOWN。
- 任务查询 `ec483bd` / `9ca6d13`：列表与分层详情均分页，组内素材显示已核实的目标 VID/封面，事件仅暴露白名单；版权方/策略绑定来自冻结预览。
- 队列执行 `7dddbe4`：unit→step 锁序、结果与下一步 outbox 同事务、当前消息修复与业务退避分开；完整成功、远端成功丢响应后回读续建、回读为空停止自动重试已通过 10 条专项测试。
- Linux 进程故障测试 `a842358` 已提交，运行状态独立记录；当前新增 Scene 编排、recovery 与 UI 不在上述已通过范围中。
## P06 recovery backend — 2026-09-09

- Added permanent tenant-scoped retry/reconcile request receipts and separate mutable recovery progress (`0010_submission_recovery`); immutable intent triggers and preview/BC composite foreign keys. Added per-step evidence index (`0011_recovery_evidence_index`).
- Added 100-step durable recovery scanning, caller/original-actor separation, current account gates, dependency-aware definitely-unsent retry, existing readback queue reuse, exact current-generation outbox repair, and SQL-derived recovery counts/buttons. Historical receipt GET remains QUEUED/0 and read-only; job COMPLETED means scan scheduling finished.
- MATERIAL reconciliation now carries strict `read_only` through the original distribution verifier and all continuations. Delayed failure cannot select a new upload; successful original receipts can revalidate their existing VID. Original frozen intent and actual source history remain intact.
- Verified on isolated PostgreSQL and actual Redis with official SDK transport doubles: 476 builds/materials regressions passed; 38 focused recovery/SDK checks passed; final 20 boundary/SDK checks passed after the last strict-read changes. Ruff, mypy, ty, Alembic upgrade/check, and diff whitespace checks passed. No real provider/TikTok/S3 operations.
- Integration: include `builds.recovery_api.router`; include `app.modules.builds.recovery_tasks` in Celery; register periodic `builds.repair_recoveries` on control; map `recovery_no_candidates` to HTTP 409. Root owns bounded terminal material-result synchronization and shared API/client registration. Details: `docs/contracts/submission-recovery.md`.

## 2026-09-09：工作区目录整理

- 应用迁入 `projects/tiktok-ad-automation/`；工作区根目录旧入口保留为兼容符号链接，供现有虚拟环境与运行进程使用。
- 修复并验证 51 个 Git worktree 的访问路径，同步应用文档中的工作区绝对路径和兄弟工具路径。业务实现未改动。
- 根目录旧设计计划整体归档至工作区 `archive/workspace-design-20260909/`；应用内设计与实施进度继续作为当前开发入口。
- 验证：Git worktree 访问、`git diff --check`、Python 虚拟环境可执行文件通过；工作区批处理、VID 接力、骏伯监控离线回归通过。未运行应用全量测试，未调用外部广告接口或发送消息。
