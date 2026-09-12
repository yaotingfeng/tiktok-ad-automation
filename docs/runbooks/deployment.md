# 发布与部署

新加坡独立测试服务器使用无 Docker 的 systemd 部署，操作遵循 [staging-singapore.md](staging-singapore.md)。

骏伯生产环境使用独立 8000 HTTPS 入口，发布与数据库操作必须遵循 [production-junbo.md](production-junbo.md)，不执行下面的通用 staging 命令。首次生产验收见 [发布记录](../validation/2026-09-10-production-release.md)。真实 TikTok 联调仍需单独完成。

其他环境的首次配置和宿主机命令见 [bootstrap-deployment.md](bootstrap-deployment.md)。下文为通用 staging 方案，补充版本、素材存储和升级顺序。

## 每次发版的配置与备份规范

首次部署、新增功能开关及日常升级，均须先执行下方[功能开关清单与发布确认](#功能开关清单与发布确认)。模板默认关闭不等于用户选择关闭；缺少配置导致功能不可用时，不能只交付“服务启动成功”。

本节适用于测试及生产的每次实际部署，包括仅前端、配置、依赖变更和没有数据库迁移的发布；仅提交文档而未部署不属于发版。首次部署没有旧版本或历史数据时记录不适用项，不伪造备份成功。

发布前按目标环境手册核对以下配置，只记录存在性、非敏感地址及检查结果，不记录密钥值：

| 配置范围 | 核对内容 |
| --- | --- |
| 运行环境 | 固定 Git SHA、构建方式、服务用户/目录、域名/端口、TLS、反向代理、前端 origin 与 CORS；API/Worker/Beat 同版本且仅一个 Beat |
| 数据与密钥 | PostgreSQL/Redis 连接与隔离、凭据加密密钥保持可解密既有数据、备份路径及可用空间 |
| 素材 | API/Worker 一致的 S3/R2 配置、私有桶、签名 URL 可达性、CORS、原件保留策略；外部对象不包含在项目归档中 |
| API 授权 | 已获批 App 的服务端配置、准确的 API callback；缺失时该通道保持不可用 |
| MCP 授权 | 独立客户端注册文件、issuer/resource、MCP callback、服务用户读取权限；注册配置与租户授权分别验收 |
| 能力与调度 | 实际通道配额和归属域、上传服务策略、功能开关、Worker 期限与正常排空时间；不套用合成测试配置 |

每次变更前冻结新写入、停止调度并正常排空任务，暂停备份 timer 并等待已启动的备份自然结束；按环境手册创建同一批次的私有备份：

1. PostgreSQL 自定义格式 dump，及与之同一停写窗口的 Redis RDB；保留队列/outbox 和待核查状态。
2. **当前运行项目的独立归档**：后端/前端源码、迁移、依赖锁文件、部署脚本与实际前端构建产物。记录归档根目录、旧/新 SHA；保留旧 release 目录仅作为便捷回退。可重建的 node_modules/虚拟环境/缓存可排除，但须记录工具链版本及可重建依赖来源。容器环境另保留不可变镜像 digest 与可恢复镜像。
3. 私有运行配置、凭据加密密钥、MCP 注册文件、该项目所有生效的 systemd/Compose/反向代理配置、证书和备份脚本；与项目归档分开受控保存，严禁入 Git 或公开下载。
4. 批次清单：时间、目标环境、运行 SHA、数据库 current/head、归档内容/排除项、文件大小及 SHA-256、恢复步骤与验证结果。完成全部检查后才标记本次发布备份完整；仅数据库备份脚本的 COMPLETE 不足以证明项目归档已完成。

检查 dump 目录、RDB 完整性、项目/配置归档可列出和校验和；在隔离临时数据库及独立 Redis 实例验证恢复，项目归档解压至独立目录核对源码和构建产物，绝不覆盖运行目录或原 AOF。备份缺项、失败或恢复验证失败时停止发布，处理后再继续。通过后才迁移、切换并验收；完成或中止恢复处理后恢复 timer。

验收记录注明备份目录、所有归档与恢复结果、当前版本/head、服务状态及真实联调边界。保留旧版本和本次完整备份直至恢复要求得到满足；未经核实不自动清理。异地复制是否已配置须如实记录，同机副本不能代替异地恢复能力。

## 素材响应归档部署与恢复

URL 导入的上传及异常恢复查询完整响应保存于 `material_response_archive`；无需另开开关，也不会恢复正常上传后的逐条回查。首次包含该功能的发布必须迁移到 `mat_response_archive`（后续以实际单 head 为准），不能只替换代码。范围、格式与失败行为见[响应留档设计](../superpowers/specs/2026-09-13-material-response-archive.md)。

沿用该环境 `CONNECTION_ENCRYPTION_KEY`，不要重新生成密钥。完整数据库备份须包含归档表，私有配置备份须包含对应密钥；隔离恢复后验证可解密及摘要一致。原件自动清理不删除响应；当前不自动按时间清理归档，监控 `pg_total_relation_size('material_response_archive')` 和备份增长。回退应用前检查兼容，禁止用迁移 downgrade 删除已有响应。

排查时按租户、BC、素材、操作和接收时间定位归档，在受控服务环境调用 `app.modules.materials.response_archive.read_material_response`，传入当前管理员的 `TenantContext`、BC、素材及响应 ID；该函数重新验证数据库权限后返回完整正文 bytes。不要直接在普通日志、页面或工单中展开密文解密内容；必要导出只放私有目录。历史未保存的上传正文不可恢复，查询响应不能冒充上传响应。归档失败错误码为 `material_response_archive_failed`；恢复数据库/密钥后核查原 UNKNOWN 操作，不直接重新上传。

## 功能开关清单与发布确认

### 当前开关及影响

以下是当前 `backend/app/core/config.py` 中的全部部署级布尔功能开关；`.env.example` 和 `compose.yml` 默认均为 false。两个开关作用于整个应用实例，覆盖该实例所有租户和 BC，不能当作某个账户的独立开关。实际业务仍受各租户/BC 权限及证据检查约束。

| 开关 | 关闭时的实际影响 | 开启前提与实际验收 |
| --- | --- | --- |
| `MATERIAL_INGEST_ENABLED` | 阻止新素材导入受理、上传权限签发及新来源账户 URL 上传；已发送操作的结果核查仍保留。页面可访问不代表能入库 | 配置私有存储、分片 CORS、容量窗口、Linux prefork Worker、Beat 和调用额度；当前租户/BC 的连接与上传权限有效。在用户授权的素材与账户范围验证接收、来源上传成功回执的有效 VID 及页面可用状态；未知结果恢复另外验证 |
| `MATERIAL_CLEANUP_ENABLED` | 不发送新的原件删除/分片终止；已发送删除继续核查。已核实来源的原件会停在 `cleanup_pending / cleanup_disabled`，持续占用容量，窗口耗尽后新文件等待容量 | 私有存储具有相应删除和读取权限，明确接受清理临时原件。成功来源回执、对象归属及无活动使用等由任务再次检查；确认 DELETE/HEAD 或分片关闭证据、页面已清理计数、预算释放及账户素材保留 |

清理开启后，已有合格积压也会处理；来源账户上传已确认成功且原件无活动使用后即可清理，没有固定保留几天的期限。后台每 30 秒检查待恢复清理任务，完成时间受任务队列、使用占用和存储响应影响。取消/闲置原件也会经各自证据检查后处理；`MATERIAL_ABANDON_SECONDS` 是闲置候选观察阈值，不是已入库素材的统一保留期。删除临时原件不会删除广告账户里的素材；重新关闭开关不能恢复已删除字节，也不等于完整跨账户分发或广告搭建已验收。

`TIKTOK_CALL_POLICIES`、`MATERIAL_REMOTE_MEDIA_HOSTS`、容量配置、OAuth/BC 绑定、实际账户上传/搭建权限也可能阻断功能，但不是这两个布尔开关。按本手册及 [R2 手册](r2-video-upload.md) 分别核对；不要为排除阻断直接伪造权限或远端能力。

### 何时向用户确认

1. **首次部署**：准备完整开关清单，逐项列明目标环境、作用范围、拟定 true/false、开启及关闭影响、缺失前提，再向用户确认。可以一次确认整张清单；用户尚未选择时，不把模板 false 当作已批准的最终配置，也不宣称相关功能可用。
2. **新增开关或已有开关语义发生变化**：在部署前说明新增功能、默认值、拟定值、是否处理历史积压，以及关闭会导致什么功能不可用，向用户确认后再应用。单纯“发布这个版本”不等于已经选择新增开关的状态。
3. **日常升级或重启，已有开关含义和值不变**：沿用该环境已确认的实际配置，不重复询问、不被模板或过期本地副本重置。已在当前会话明确确认目标环境和开关值的，直接按授权执行；不能因换版本重复要求确认。
4. **拟改变已有开关值**：若用户本次尚未明确授权该变更，说明影响后确认；用户明确要求“开启自动清理”等，已构成对应环境与范围内的授权。首次部署/新增开关缺少答复时，继续完成无需该决定的准备工作，保留依赖该决定的部署步骤待确认，不以超时或默认选项代替答复。

确认应在差异清单、依赖检查和验证方案准备完整后，且在停止服务或应用配置前完成。示例：新加坡测试环境拟启用素材导入及原件清理，作用于实例内所有租户；已核验且无占用的现有原件会开始删除，账户素材保留；列明已具备的前提、尚待真实验收的部分，再请用户确认这两个值。

### 每次部署的执行与留证

1. 对比目标版本的 Settings、`.env.example`、Compose/启动注入和服务器实际配置，列出新增、缺失、删除及语义变化项。每个开关记录“当前生效值 → 本次拟定值 → 用户确认依据”；首次部署的当前值记为未部署，无法核实的记为待核实，不能套用其他环境的值。
2. 完成上节确认后，按完整备份和排空流程变更。部署私有环境文件显式写出已确认的每个开关值，不依赖代码或 Compose 默认值。测试配置位于 `/etc/tt-ada-staging/app.env`，生产位于 `/etc/tt-ada/production.env`；同步受控副本，禁止模板覆盖真实配置。
3. 测试环境重启 API、所有 Worker、唯一 Beat；生产使用固定版本 `deploy/production-compose.sh` 重建受影响服务容器，单纯 restart 不会更新 Compose 环境。分别核对所有运行进程/容器的实际值、版本和同一份配置来源，不能只检查磁盘文件。
4. 在各服务实际环境、用户和工作目录中核验 Settings 的最终布尔值；日志只输出开关白名单和一致性结论，不输出整个环境或展开后的 Compose 配置。上述步骤尚为人工发布要求，现有启动脚本不会自动代替用户确认。
5. 按清单中的业务路径验收。开关 true、服务健康、MCP READY 分别只证明各自状态，不能代替真实业务完成；外部操作超出已有授权时记录待验范围。若用户选择保持关闭，交付时明确列出不可用功能、积压/容量影响及后续开启步骤。
6. 发布记录保存目标环境/版本、每项发布前后值、确认依据、备份批次、各服务生效检查和实际业务结果。更新环境手册与实施进度；回退时重新核对配置含义与数据库兼容性，不因切换旧代码静默重置已确认开关。

### 新开关开发完成的文档要求

每次新增、移除、改名、调整默认值或变更作用范围，同一提交同步更新 Settings、`.env.example`、运行注入位置、本节清单、功能专用手册和实施进度；记录对现有环境及积压任务的影响。发布前列出需要用户选择的具体值。开关必须说明作用范围、依赖、关闭表现、开启/关闭后在途任务行为、服务重载方式及成功证据，避免开发后长期停在默认关闭却被当作已交付。

## 调用额度配置与交付门槛

`TIKTOK_CALL_POLICIES` 是系统通过 Redis 执行的请求限流和并发策略，不是付费额度或广告余额。API 与 MCP 都依赖它：为空 `{}` 时业务请求会被 `admission_unconfigured`（“请配置应用调用额度”）阻断，格式或租约不合法会产生 `admission_policy_invalid`。**启用任一通道前必须完成配置和校验，不能只部署代码、等用户授权后才补配置。**

配置由部署管理员写入目标环境私有环境文件：测试 systemd 使用 `/etc/tt-ada-staging/app.env`，骏伯生产使用 `/etc/tt-ada/production.env`，其他 Compose 环境按对应手册指定文件。API、所有 Worker 和 Beat 必须加载相同策略及同一业务 Redis；仅修改本地 `.env` 或文件而未重启已有服务不算生效。Compose 的空值默认用于允许基础系统启动，**不代表 TikTok 集成已经配置完成**。

以下为当前测试环境的完整单行写法，不含凭据；其他环境先根据已核实的通道限制和负载制定策略，不直接当作官方默认值：

```dotenv
TIKTOK_CALL_POLICIES='{"base":{"app_max_inflight":4,"endpoint_max_inflight":2,"tenant_max_inflight":4,"advertiser_max_inflight":4,"app_calls_per_window":10,"endpoint_calls_per_window":3,"window_ms":1000,"lease_ms":960000},"endpoints":{}}'
```

| 字段 | 含义与测试值 |
| --- | --- |
| app_calls_per_window / window_ms | 每共享配额域每 1000 毫秒最多 10 次请求；为本地总量控制 |
| endpoint_calls_per_window | 每逻辑操作每窗口最多 3 次请求 |
| app_max_inflight / endpoint_max_inflight | 每共享配额域总并发 4、每逻辑操作并发 2 |
| tenant_max_inflight / advertiser_max_inflight | 同配额域内每租户/每广告账户各最多 4 个并发请求 |
| lease_ms | 并发占用租约 960000 毫秒，用于异常退出后的回收，不是请求超时或等待时长 |
| endpoints | 按实际逻辑操作名覆盖 endpoint_max_inflight、endpoint_calls_per_window、lease_ms，不能覆盖共享总量 |

操作键及上传租约要求见下文“准入配置键迁移”；上传租约必须大于 905000 毫秒，候选目录读取租约也必须覆盖其整个请求时限。未知 MCP 主体时 **MCP_SERVICE_QUOTA_SCOPE 不设置**，代码将所有 MCP 连接合并至保守共享域；不能用租户、BC、connection_id 或 attempt_id 拆分额度，也不要写空字符串。只有核实真实上游配额归属后才设置该项。API 使用真实 App ID 的配额域；没有 API App 不影响 MCP 使用共享域。

在目标服务用户、工作目录、解释器和实际环境变量下执行以下无网络、无业务写入检查（不能用开发机或演示配置的通过结果替代）：

```python
from app.jobs.admission import admission_policy

operations = (
    "protocol.initialize", "protocol.list_tools", "accounts.list_bcs",
    "auth_refresh", "materials.upload_video_file", "materials.upload_video_url",
)
for operation in operations:
    policy = admission_policy(operation)  # 同时验证 base 和所有覆盖项
    minimum = 905000 if operation.startswith("materials.upload_video_") else 50000
    assert policy.lease_ms > minimum, "调用租约不足以覆盖请求期限"
print("PASS: 调用额度配置与租约校验")
```

交付检查必须按顺序留证：

1. 发布前完成私有配置、策略解析/租约、Redis 可达性、各服务配置来源检查；缺项先修复。配置更新也须执行完整备份、排空和服务重启，按目标环境手册操作。
2. 发布后确认 API/Worker/Beat 均已重新加载配置、版本一致，执行健康、登录与隔离检查。健康和 MCP configuration READY 不检查完整调用链，不能作为额度已配置的替代证据。
3. 在用户授权范围内完成 OAuth 换码、实际 initialize/tools/list、候选 BC 列表读取；由用户选择绑定 BC，再验账户发现。需要用户登录/选择时保留待验状态，不代其授权或猜测 BC。
4. 记录“已配置并生效”“只读已验证”“尚待用户授权/绑定”“素材/广告未验”等实际结果。额度缺失、解析失败或候选读取失败时，不能交付为 MCP 已可用；上传和广告创建仍须独立验收。

本节是部署执行规范，不能假设现有启动脚本已自动检查所有项目；部署者须完成上述步骤并记录结果。

## 同一 BC 的双通道选择

API（OFFICIAL_API）与 MCP（OFFICIAL_MCP）按独立 connection_id 保存授权，同一租户的同一 BC 可分别绑定两类连接，授权记录不会在系统内互相覆盖。新操作明确指定连接时使用该连接，否则使用租户管理员为该 BC 设置的唯一默认连接；没有默认连接时要求选择，不自动优先 API/MCP，也不采用“最后授权者优先”。

首次 MCP 绑定通过完整目录验证后，如果该 BC 尚无默认路由，发布事务会将当前连接初始化为默认；已有默认不会被覆盖。上述“没有默认时要求选择”指运行时选路规则，不能理解为首次绑定不会初始化默认。授权回调与 BC 接入分开：一次 MCP 授权列出当前账号可访问的完整 BC 目录，管理员可多选接入；只有一个 BC 时自动勾选，仍提交确认。已有授权使用“添加 BC／刷新列表”发现后续新增 BC，无需重新 OAuth。各 BC 独立同步、重试、解绑和设置默认连接；停用整份连接会影响该连接下全部 BC。解绑会使该 BC 原有冻结任务失效，重新接入也不会恢复这些旧任务；普通同步和令牌轮换不改变绑定代数。

任务冻结连接及授权/接口版本后，执行、重试和结果核查沿用原连接。改默认只影响后续新选路，已冻结任务不迁移；原连接不可用时阻断，不自动切到另一通道重复创建。两条连接操作的是同一 TikTok BC 的资产，并非两套广告数据；切换默认本身不会复制广告。应用审核通过后可先独立授权、验证 API，再由管理员将 BC 默认改为 API。

## 固定发布版本

从通过验收的 Git SHA 构建镜像，将私有部署环境的 `APP_IMAGE` 设置为不可变 tag 或 registry digest。API、prestart、Worker、Beat 必须使用同一个值；保留上一个镜像和与之匹配的数据库备份。发布文件为 `compose.yml` 与 `compose.staging.yml`，不要混入本地 override。

在正式部署主机、已配置私有 `.env` 和 `deploy/traefik.yml` 后执行：

```bash
docker compose -f compose.yml -f compose.staging.yml config --quiet
docker compose -f compose.yml -f compose.staging.yml build prestart
docker compose -f compose.yml -f compose.staging.yml up -d db redis
docker compose -f compose.yml -f compose.staging.yml run --rm --no-deps prestart
docker compose -f compose.yml -f compose.staging.yml up -d --no-build backend worker beat proxy
docker compose -f compose.yml -f compose.staging.yml ps
```

使用 registry digest 时提前 pull 对应镜像，省略 build。升级已有业务实例时，先停止接收新提交并正常排空 Worker，再备份和迁移；不要在旧 Worker 仍执行时替换数据库结构。迁移失败时不启动新 API/Worker。已有广告不会随本地服务停止而停止投放。

只启动一个 Beat。Worker 使用 Linux prefork；素材上传任务最长 900 秒，广告创建/核查与资源读取任务最长 45 秒。优雅关机应给现有任务完成时间；异常退出由持久化租约和核查处理。`--pool solo` 仅用于基础本地诊断，业务任务会拒绝缺少硬期限的执行环境。

Beat 每秒触发一次 outbox 发布。单次发布默认最多 20 个公平轮次，或到达 1 秒预算后结束当前轮；每轮单独提交数据库事务，全局最多 100 条、每租户最多 5 条，其中最多一条提交展开任务。没有成功发布时立即结束，保留原消息退避。可通过 `DISPATCH_MAX_ROUNDS`（1～100）及 `DISPATCH_TIME_BUDGET_SECONDS`（大于 0、至多 5）调整本地发布预算。默认单租户的每次调度数量上限为 100 条，这只是数量上限，实际速度受数据库、Broker 和 Worker 影响；TikTok 两通道配额仍由每次真实请求的共享准入独立控制。控制任务有 30 秒硬期限，不能据此假设发布速度等于广告创建速度。

## 私有素材存储

对象桶保持私有，配置 `S3_*` 和原件持久化/备份策略。API 与 Worker 使用相同 endpoint、bucket、region 和凭据；浏览器必须能访问签名 URL 中的存储地址，不能使用仅容器内可解析的 hostname。页面只通过有时效的签名 URL 直传分片和查看原件。

将 [s3-cors.example.json](../../deploy/s3-cors.example.json) 中的 origin 改为实际前端 origin，再通过存储管理界面配置。示例允许 PUT/GET/HEAD 和所需请求头，向浏览器暴露 ETag 以保存分片回执；它不会设置公开读权限。S3 控制台使用 JSON 数组格式；兼容存储以其自身配置格式为准。[AWS CORS 字段说明](https://docs.aws.amazon.com/AmazonS3/latest/userguide/ManageCorsUsing.html)

默认应用工程限制：SDK 单文件上传 256 MiB、同 App 大文件上传并发 1、目标素材核验缓存 900 秒。超过 SDK 限制时保留已上传原件并显示阻断原因；这不是官方平台文件上限。BC 能力与场景证据默认 24 小时，部署时应结合完整账户目录和队列耗时配置，不把它们当成 API 额度。

## 发布核查

在 backend 容器执行 `alembic heads` 与 `alembic current`，确认单一 head 且匹配版本。对实际 HTTPS origin 运行仓库 `scripts/check-bootstrap.py`，再验证管理员登录、租户切换与 `/api` 同源访问。健康端点仅证明 API 进程响应；数据库和 Redis 分别看 Compose health，消费者用 `celery inspect ping` 验证。

官方 API 的回调地址为 `https://实际域名/api/integrations/tiktok/callback`，取得 API App 后填写实际配置并从租户连接页授权；未配置时的错误响应不等于 OAuth 成功。MCP 独立使用 `https://实际域名/api/integrations/tiktok/mcp/callback`，无需 API App，但必须有已核实的 MCP 客户端注册、固定 issuer/resource 与合法 callback 配置，由租户管理员自行 PKCE 授权并绑定 BC。注册材料保存在受控本地文件，凭据仅服务端加密保存，不复用 Codex token 或浏览器缓存。实际入口含非默认端口时，callback 必须逐字包含端口。

真实 API 与 MCP 能力分别记录在 [live-sdk.md](../acceptance/live-sdk.md) 和 [live-mcp.md](../acceptance/live-mcp.md)，两者互不证明。

故障观察与备份恢复见 [recovery.md](recovery.md)。日志使用应用白名单，不打开 HTTP request body、OAuth query、浏览器登录会话或展开后的 Compose 环境输出。


## 双通道版本发布关卡

2026-09-11 双通道任务的 [离线记录](../validation/2026-09-11-tiktok-dual-channel-offline.md) 已记录本地后端 2203 passed / 9 skipped 与前端 348 passed；此前跳过的 8 个 Linux prefork 用例及 1 个相关回归已在 2026-09-12 测试服务器通过，见 [发布验收](../validation/2026-09-12-staging-mcp-release.md)。真实租户授权及业务协议仍待验；测试环境发布不构成生产发布授权。执行发布前按实际环境逐项补齐证据。

1. 固定审查通过的完整 Git SHA、镜像 digest、迁移路径和唯一目标 head。多 BC 版本 head 为 `mcp_multibc_runtime`；以实际发布版本的 `alembic heads` 为准，不在服务器生成或修改迁移。
2. 冻结新写入，排空当前执行任务；再停止调度与消费者。保留队列/outbox、原请求和所有 UNKNOWN，不能以清空它们代替排空。暂停备份 timer 并等待已经开始的备份自然结束，随后创建并验证可恢复备份。
3. 使用新固定版本执行审查后的迁移。确认 current=head、单 head 和历史证据保留；API/prestart/worker/beat 必须同一 SHA。旧路由无法证明时应保持阻断，不用今日默认连接补历史。默认连接有歧义时由管理员明确选择，不能自动择一。
4. 先验管理员/租户/BC 隔离、连接状态、只读与旧 API 路径，再按已有独立证据开放 MCP 能力。实际 scope、主体或 tools/list/schema 未核实，禁止开启写能力；MCP URL 上传按已接入合同、实际账户权限及原件回读校验，见 [2026-09-13 修复记录](../validation/2026-09-13-mcp-material-upload.md)。不得把测试 SYNTHETIC 注册或策略复制到部署配置。
5. Linux prefork 使用真实 PG/Redis 执行接收后断线/终止及原通道读取恢复，确认总 create 次数为 1。macOS skip 不构成此项通过。读取 CTA 缺实际账户字段时维持 INCOMPLETE，不降低合同来取得绿色结果。
6. 成功切换或完成中止恢复后恢复备份 timer，记录当前版本、head、服务和验证结果。回退应用前先核实 schema 兼容；持久化的冻结 route、原始摘要和回执不能为降级而删除，优先修复前进。

具体命令与停止顺序以目标环境手册为准；骏伯只能使用该版本 `deploy/production-compose.sh`，不要运行上面的通用 staging Compose 命令。


## 准入配置键迁移

双通道 gateway 使用逻辑操作键查询 `TIKTOK_CALL_POLICIES.endpoints`，当前操作分组为 `accounts.*`、`scene.*`、`materials.*`、`build.*` 和 `protocol.*`（以发布版本的实际 operation 名称为准）。以上指网关业务/协议键；独立 MCP 刷新仍使用 `auth_refresh`，不将它改名为点分前缀。升级时逐项迁移原 SDK URL 路径覆盖；旧 URL 条目不会命中，也没有兼容回退。共享 base 与配额归属域继续生效，不能通过改键绕过共享额度。

例如旧 `/open_api/v1.3/file/video/ad/upload/` 覆盖需分别配置 `materials.upload_video_file` 与 `materials.upload_video_url`。二者当前 worker hard limit 为 900 秒，lease 必须严格大于 hard limit 加 5 秒，即 **大于 905000ms**；仅保留旧 URL 条目会落到 base，短 lease 会被门禁拒绝。容量/频率/并发值应依据目标环境及实际已核实的通道额度设置，测试里的 970000ms 和宽松 quota 不是官方生产默认。配额配置正确仍不能替代账户权限及实际上传回读验收。


## MCP 多 BC 升级验收

按 [多 BC 设计](../superpowers/specs/2026-09-12-mcp-multiple-bcs-design.md) 发布。迁移 `mcp_multi_bc` 和 `mcp_multibc_runtime` 保留原连接 ID、凭据、账户和不可变任务；旧已完成授权的接受时间从原目录完成回执补齐。旧运行目录任务只从自身冻结路由回填 BC 与版本，无法证明的任务保留证据并标为失效，不能使用当前默认连接补造历史。已有绑定或冻结证据时禁止强制降级删除新增列。

发布后先核对原授权、BC 默认及账户数量，再使用现有连接的“添加 BC／刷新列表”验证实际 tools/list 和当前账号可访问 BC；此步骤不要求用户再次登录。随后对已明确接入的 BC 执行一次同步，确认其他 BC 不受影响、聚合进行中数量最终归零。没有第二个真实 BC 时，应记录多 BC 并发仅由隔离测试覆盖，不写成真实多 BC 联调成功。仅共享授权刷新不代表每个 BC 的账户目录都变新；目录时效必须按 BC 和原授权／绑定版本分别核对。
