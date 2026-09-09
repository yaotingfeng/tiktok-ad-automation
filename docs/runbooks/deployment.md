# 发布与部署

首次配置和宿主机命令见 [bootstrap-deployment.md](bootstrap-deployment.md)。本手册补充版本、素材存储和升级顺序。配置校验、镜像 CI 与真实公网部署分别记录，当前没有公网部署或真实 TikTok 联调证据。

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

Beat 每秒触发一次 outbox 发布。单次发布默认最多 20 个公平轮次，或到达 1 秒预算后结束当前轮；每轮单独提交数据库事务，全局最多 100 条、每租户最多 5 条，其中最多一条提交展开任务。没有成功发布时立即结束，保留原消息退避。可通过 `DISPATCH_MAX_ROUNDS`（1～100）及 `DISPATCH_TIME_BUDGET_SECONDS`（大于 0、至多 5）调整本地发布预算。默认单租户的每次调度数量上限为 100 条，这只是数量上限，实际速度受数据库、Broker 和 Worker 影响；TikTok API 配额仍由每次真实请求的共享准入独立控制。控制任务有 30 秒硬期限，不能据此假设发布速度等于广告创建速度。

## 私有素材存储

对象桶保持私有，配置 `S3_*` 和原件持久化/备份策略。API 与 Worker 使用相同 endpoint、bucket、region 和凭据；浏览器必须能访问签名 URL 中的存储地址，不能使用仅容器内可解析的 hostname。页面只通过有时效的签名 URL 直传分片和查看原件。

将 [s3-cors.example.json](../../deploy/s3-cors.example.json) 中的 origin 改为实际前端 origin，再通过存储管理界面配置。示例允许 PUT/GET/HEAD 和所需请求头，向浏览器暴露 ETag 以保存分片回执；它不会设置公开读权限。S3 控制台使用 JSON 数组格式；兼容存储以其自身配置格式为准。[AWS CORS 字段说明](https://docs.aws.amazon.com/AmazonS3/latest/userguide/ManageCorsUsing.html)

默认应用工程限制：SDK 单文件上传 256 MiB、同 App 大文件上传并发 1、目标素材核验缓存 900 秒。超过 SDK 限制时保留已上传原件并显示阻断原因；这不是官方平台文件上限。BC 能力与场景证据默认 24 小时，部署时应结合完整账户目录和队列耗时配置，不把它们当成 API 额度。

## 发布核查

在 backend 容器执行 `alembic heads` 与 `alembic current`，确认单一 head 且匹配版本。对实际 HTTPS origin 运行仓库 `scripts/check-bootstrap.py`，再验证管理员登录、租户切换与 `/api` 同源访问。健康端点仅证明 API 进程响应；数据库和 Redis 分别看 Compose health，消费者用 `celery inspect ping` 验证。

App 申请前可以部署登录和回调入口。域名配置完成后，回调地址为 `https://实际域名/api/integrations/tiktok/callback`。取得 App 后再填写其实际配置和已核实额度，从租户连接页面授权。没有 App 时的确定错误响应不等于 OAuth 成功。真实能力与试投逐项记录在 [live-sdk.md](../acceptance/live-sdk.md)。

故障观察与备份恢复见 [recovery.md](recovery.md)。日志使用应用白名单，不打开 HTTP request body、OAuth query、浏览器登录会话或展开后的 Compose 环境输出。
