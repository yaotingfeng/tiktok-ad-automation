# 发布与部署

新加坡独立测试服务器使用无 Docker 的 systemd 部署，操作遵循 [staging-singapore.md](staging-singapore.md)。

骏伯生产环境使用独立 8000 HTTPS 入口，发布与数据库操作必须遵循 [production-junbo.md](production-junbo.md)，不执行下面的通用 staging 命令。首次生产验收见 [发布记录](../validation/2026-09-10-production-release.md)。真实 TikTok 联调仍需单独完成。

其他环境的首次配置和宿主机命令见 [bootstrap-deployment.md](bootstrap-deployment.md)。下文为通用 staging 方案，补充版本、素材存储和升级顺序。

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

2026-09-11 双通道任务的 [离线记录](../validation/2026-09-11-tiktok-dual-channel-offline.md) 已记录本地后端 2203 passed / 9 skipped 与前端 348 passed；真实协议/授权及 8 个 Linux prefork 用例仍待验；当前代码实现不能自动解释为本次已获生产发布授权。执行发布前按实际环境逐项补齐证据。

1. 固定审查通过的完整 Git SHA、镜像 digest、迁移路径和唯一目标 head。双通道候选最终 head 为 `mcp_cover_evidence`；以实际发布版本的 `alembic heads` 为准，不在服务器生成或修改迁移。
2. 冻结新写入，排空当前执行任务；再停止调度与消费者。保留队列/outbox、原请求和所有 UNKNOWN，不能以清空它们代替排空。暂停备份 timer 并等待已经开始的备份自然结束，随后创建并验证可恢复备份。
3. 使用新固定版本执行审查后的迁移。确认 current=head、单 head 和历史证据保留；API/prestart/worker/beat 必须同一 SHA。旧路由无法证明时应保持阻断，不用今日默认连接补历史。默认连接有歧义时由管理员明确选择，不能自动择一。
4. 先验管理员/租户/BC 隔离、连接状态、只读与旧 API 路径，再按已有独立证据开放 MCP 能力。实际 scope、主体或 tools/list/schema 未核实，禁止开启写能力；MCP 视频五字段服务 policy 未齐全时保持关闭。不得把测试 SYNTHETIC 注册或策略复制到部署配置。
5. Linux prefork 使用真实 PG/Redis 执行接收后断线/终止及原通道读取恢复，确认总 create 次数为 1。macOS skip 不构成此项通过。读取 CTA 缺实际账户字段时维持 INCOMPLETE，不降低合同来取得绿色结果。
6. 成功切换或完成中止恢复后恢复备份 timer，记录当前版本、head、服务和验证结果。回退应用前先核实 schema 兼容；持久化的冻结 route、原始摘要和回执不能为降级而删除，优先修复前进。

具体命令与停止顺序以目标环境手册为准；骏伯只能使用该版本 `deploy/production-compose.sh`，不要运行上面的通用 staging Compose 命令。


## 准入配置键迁移

双通道 gateway 使用逻辑操作键查询 `TIKTOK_CALL_POLICIES.endpoints`，当前操作分组为 `accounts.*`、`scene.*`、`materials.*`、`build.*` 和 `protocol.*`（以发布版本的实际 operation 名称为准）。以上指网关业务/协议键；独立 MCP 刷新仍使用 `auth_refresh`，不将它改名为点分前缀。升级时逐项迁移原 SDK URL 路径覆盖；旧 URL 条目不会命中，也没有兼容回退。共享 base 与配额归属域继续生效，不能通过改键绕过共享额度。

例如旧 `/open_api/v1.3/file/video/ad/upload/` 覆盖需分别配置 `materials.upload_video_file` 与 `materials.upload_video_url`。二者当前 worker hard limit 为 900 秒，lease 必须严格大于 hard limit 加 5 秒，即 **大于 905000ms**；仅保留旧 URL 条目会落到 base，短 lease 会被门禁拒绝。容量/频率/并发值应依据目标环境及实际已核实的通道额度设置，测试里的 970000ms 和宽松 quota 不是官方生产默认。配额配置正确仍不能替代 MCP 视频服务五项能力证据。
