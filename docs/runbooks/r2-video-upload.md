# R2 视频导入部署与恢复

新导入与自动删除的模板默认均关闭，实际部署值须按[功能开关清单与发布确认](deployment.md#功能开关清单与发布确认)由用户选择，不能静默沿用默认值并交付为功能可用。两个开关均作用于整个部署实例；验收素材可限定租户、BC 和小批范围，但开关本身不提供单租户或单批隔离。首次部署及新增开关先说明前提、影响和拟定值，再确认；普通升级保持已确认值。不能复用历史运营目录里的账户、Token 或存储密钥。部署变量注入 API、Worker、Beat；只记录开关布尔值及检查结论，不记录私密配置。

正常 URL 上传现按[成功回执决策](../superpowers/specs/2026-09-13-upload-success-receipt.md)直接入库：收到业务成功和实际 VID 即结束来源上传，不固定等待 60 秒或逐条查询；下面的独立回读验收用于未知结果恢复及目标跨账户路径，不能再次加到每条成功上传上。

## 配置

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `OBJECT_STORAGE_PROVIDER` | `s3` | 本导入流程部署时设 `r2`；旧 S3 入口保留兼容 |
| `S3_ENDPOINT_URL` | 空 | 私有账户的 HTTPS S3 API endpoint，形如 `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`；不能带用户名、路径或签名参数 |
| `S3_BUCKET` | 空 | 已明确授权使用的私有桶；脚本不创建桶或修改桶权限 |
| `S3_REGION` | `us-east-1` | 旧 S3 配置；R2 客户端实际使用 `auto` |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | 空 | 仅服务端；限制到本应用所需桶和对象操作，不写入浏览器配置 |
| `MATERIAL_INGEST_ENABLED` | `False` | 允许新导入受理、Create、签名、Complete；关闭后保留已有发送记录的只读回查 |
| `MATERIAL_CLEANUP_ENABLED` | `False` | 独立控制新自动删除；已发送删除的核实仍需运行 |
| `MATERIAL_URL_MAX_UPLOAD_BYTES` | `1073741824`（1 GiB / 1024 MiB） | 本次 URL 上传本地保护上限；不代表 TikTok 官方上限或大文件验收通过 |
| `MATERIAL_STORAGE_GLOBAL_BYTES` | `8589934592`（8 GiB） | 所有租户合计的原件瞬时预留窗口 |
| `MATERIAL_STORAGE_TENANT_BYTES` | `2147483648`（2 GiB） | 同租户所有 BC 合计窗口，不能按 BC 再获得一份额度 |
| `MATERIAL_PART_URL_SECONDS` | `900` | 分片签名有效期，60–900 秒 |
| `MATERIAL_INGEST_URL_SECONDS` | `7200` | 平台读取原件 GET 签名有效期，60–7200 秒；不是远端任务终止证明 |
| `MATERIAL_VALIDATION_SECONDS` | `300` | 原件读取/摘要/媒体校验时间界限，30–600 秒 |
| `MATERIAL_ABANDON_SECONDS` | `86400` | 闲置候选观察时间，最小3600秒；不授权按年龄直接删除活跃或未知用途 |
| `MATERIAL_REMOTE_MEDIA_HOSTS` | 空集合 | 经当前官方媒体返回值和安全校验确认的精确 HTTPS 媒体主机集合；部署前单独验证，不填宽泛通配符或业务网页域名 |
| `MATERIAL_SDK_MAX_UPLOAD_BYTES` | `268435456` | 既有 SDK 文件上传内存保护；不能当作官方 URL 上传约束 |
| `MATERIAL_SDK_UPLOAD_MAX_INFLIGHT` | `1` | 既有文件 SDK 的进程内上限；不等于整机/全租户吞吐保证 |

`reserved_bytes` 是整个已准入文件的占用，直到删除/分片关闭得到可靠证据后释放；`stored_bytes` 是其中已完整接收的子集，不能把二者相加计算配额。等待容量的已受理文件保留名称、大小、client_index 等元数据，但不发 Create 或签名。关闭弹窗/刷新不代表可以释放额度。

每代原件固定 provider、endpoint、bucket、tenant、BC、material、generation 和对象键。更换部署桶或账户不能使旧原件自动改用新桶清理。历史 namespace 未知对象需显式归属与摘要验证，不能凭迁移把它们变成可删除对象。

## Linux 服务布局

生产使用 Linux Celery prefork。资源、搭建、控制分别消费 `resources`、`builds`、`control` 三个队列，并连接同一套 Redis 准入状态；Beat 只能有一个实例。示例命令从 backend 工作目录执行，进程数按实测 CPU、内存和平台配额配置：

```sh
celery -A app.jobs.celery_app:celery_app worker --pool=prefork -Q resources --concurrency=2 --hostname=resources@%h --loglevel=INFO
celery -A app.jobs.celery_app:celery_app worker --pool=prefork -Q builds --concurrency=2 --hostname=builds@%h --loglevel=INFO
celery -A app.jobs.celery_app:celery_app worker --pool=prefork -Q control --concurrency=1 --hostname=control@%h --loglevel=INFO
celery -A app.jobs.celery_app:celery_app beat --schedule=/var/run/celery/celerybeat-schedule --loglevel=INFO
```

模块 imports 必须包含 `app.modules.materials.ingest_tasks`、`validation_tasks`、`cleanup_tasks` 和既有素材任务。确认 control 有 `materials.repair_ingest_transports`（建议30秒、每轮≤100）及对应校验/清理恢复任务；`materials.reconcile_ingest_transport` 投递 resources。具体任务 hard/soft limits 由代码配置，不能通过 eager、线程池或 Mac solo 声称完成了生产硬超时验证。API 控制面 boto connect/read timeout 也不是 DNS 或整个请求的硬时限。

当前生产 Dockerfile 已安装 ffmpeg，校验使用 ffprobe；部署镜像仍应执行 `ffprobe -version` 并核对镜像摘要。摘要校验 Worker 会有界读取 R2 原件并使用可清理临时文件，API 不转发视频字节。监控资源 Worker RSS、临时磁盘、超时与退出后临时文件清理；不能宣称整个服务从不读取视频。

`materials.scan_abandoned_objects` 每30秒在control扫描至多100个对象。游标在数据库提交后由Redis持有者校验写入，空页重新开始；扫描锁75秒、任务硬限45秒，旧进程不能覆盖新游标。关闭自动清理开关也停止新放弃对象扫描。依据最近实际活动时间、所有权和消费者证据判断资格，发现UNKNOWN不会按年龄删除。重复同一异常证据只记一次审计。

人工对账入口为 `python scripts/reconcile-r2.py --help`，默认只读；仅在明确tenant、BC及对象命名空间后使用对应参数。未知归属只报告，不能用扫描结果自动认领历史文件。桶级未完成分片生命周期只能作为另行配置的兜底，应用不会自动修改桶生命周期，也不对完整对象设置统一过期删除。

## 私有桶和 CORS

关闭桶公开访问，包括开发用公开访问地址。使用应用真实前端 origin 替换下面的保留示例，不使用 `*`。浏览器分片 PUT 需要读到 ETag；Content-Length 由浏览器发送并与服务端签名绑定，业务代码不自行设置浏览器禁止的请求头。CORS 仅控制浏览器跨源访问，不代替私有权限或签名授权。

```json
[
  {
    "AllowedOrigins": ["https://console.example.invalid"],
    "AllowedMethods": ["PUT", "GET", "HEAD"],
    "AllowedHeaders": ["Content-Type", "Content-Length", "Range"],
    "ExposeHeaders": ["ETag", "Content-Length", "Content-Range", "Last-Modified"],
    "MaxAgeSeconds": 300
  }
]
```

R2 不支持 S3 对象 ACL 头；本应用不发送 `ACL=private`。操作前使用 [Cloudflare S3 兼容矩阵](https://developers.cloudflare.com/r2/api/s3/api/) 核对当前 API 与头部支持，并在目标部署浏览器做实际 CORS 验收。

## 配置检查和受控存储探测

从仓库根目录、使用已安装 backend 依赖的 Python 运行。脚本只读取当前进程环境变量，不搜索历史目录、不自动加载其他 `.env`，不打印值或完整签名 URL。

```sh
python scripts/check-r2.py
```

默认只读，不初始化存储客户端，也不连接桶。退出0表示所需配置格式完整；不表示权限、桶私有性、CORS、媒体链路或吞吐已验证。缺失/无效配置输出字段名并退出1。

仅在明确授权当前 R2 账户/桶后，单独执行：

```sh
python scripts/check-r2.py --probe
```

`--probe` 只对新的随机 `tiktok-ad-automation/probes/r2/<run_id>/payload.bin` 执行 CreateMultipartUpload、签名小 PUT、ListParts、Complete、HEAD、签名 GET、DELETE、HEAD404。只清理本次 key 与已知 uploadId，绝不列举整个桶、创建桶、改策略或碰既有对象。探测输出 run_id 和阶段布尔事实，保留此 JSON 作为部署证据。签名 PUT/GET不测试实际浏览器 CORS；认证成功也不证明匿名访问被拒绝。

异常时 finally 尝试关闭本次未完成 multipart 并删除本次 key；未知 Create 没有 uploadId、未知 PUT/Complete、删除失败或 HEAD 不能核实，都可能输出 cleanup=pending。不要把 Abort响应/HEAD404/签名过期单独当作所有在途分片已终止。pending 只按该 run_id 的精确对象范围后续核查，不通过扫描全桶清理历史数据。

## 小批实际验收与放量

1. 记录当前部署版本、私有桶/CORS配置、密钥来源、实际租户与BC授权、合法来源范围。两个开关仍关闭。完成只读检查与授权后的单对象 `--probe`；检查日志没有密钥、Authorization 或完整签名 URL。
2. 只对明确小批范围开启新导入，保持自动清理关闭。验证浏览器分片、暂停/重新选择/刷新、ContentLength、摘要和媒体校验。确认同名同大小文件仍按client_index/本地内容指纹分辨。
3. 用固定官方 Python SDK验证 URL 源上传，保存实际 source advertiser、VID/MID 和摘要证据；回读当前授权和 HTTPS媒体URL精确主机。媒体 allowlist 当前有效性仍需实测，不能拿历史域名替代。
4. 证明一个已授权目标的分享或授权源 URL 转存及封面回读。先为这批源素材建立可用的后续目标路径，再小范围开启清理。
5. 等可靠 DELETE/HEAD证据和预算释放后，确认 MaterialFile/AccountMaterial、文件名、来源、VID/MID、摘要仍在。随后使用此前未分发的新目标验证删除原件后的真实路径，确认没有读已删除的 R2原件。
6. 在相同授权范围演练 API/Worker重启、响应丢失、签名续期、Token更换、限流与取消。已发送未知任务只回查，不切路径重发。验证硬超时、回查队列、清理积压与容量恢复，再逐步增大并发和日量。
7. 单文件产品上限为 1 GiB（1073741824 字节），浏览器、后端、Compose 和配置检查脚本使用同一默认值。升级时将已有环境中的 `MATERIAL_URL_MAX_UPLOAD_BYTES` 显式更新为 `1073741824`，否则旧值会覆盖新默认值。旧 FILE SDK 的 256 MiB 内存保护不变，新批量上传及目标账户分发走 URL 路径。1 GiB 真实 R2/TikTok 联调仍需单独记录；边界与合成元数据测试不替代真实网络、平台配额、内存或日吞吐验收。每个并发原件校验任务至少需要容纳 1 GiB 文件的临时磁盘并留余量；租户默认 2 GiB 暂存窗口可容纳两个上限文件，其余等待清理释放容量。

## 回滚和未知结果

先将 `MATERIAL_INGEST_ENABLED=False`、`MATERIAL_CLEANUP_ENABLED=False`，停止新权限、新创建/完成和新删除。保留已有发送记录的 readback消费者及单Beat：完整代次和发送证据允许 exact ListMultipartUploads/HEAD回查，不允许补发 Create/Complete。源/目标/删除已有 UNKNOWN 同样保留原 operation 身份回查，不因 Token变化、URL过期或进程重启换路径重发。

Cancel只先关闭浏览器继续接收并安排清理。仍有未知平台读取、原件使用记录、在途 PUT或运输 claim时，额度继续占用。对真正已完成的原件，Complete+HEAD可关闭part uses；未完成 PUT不能因为URL TTL到期就自动结束。AWS说明在途part停止后仍可能迟到，并且ListParts不返回尚未完成的part，因此普通取消的释放需要可靠的请求结束和对象/分片关闭证据；R2具体终止语义是上线验证项。[AWS multipart说明](https://docs.aws.amazon.com/AmazonS3/latest/userguide/mpuoverview.html)

每次分片签名有独立 `request_id` 和权限记录，一个权限最多发一次PUT。请求身份先保存到IndexedDB，再调用签名API；重试使用新权限，旧的unknown不被覆盖。服务端同时核对实际SigV4到期时间不超过该权限台账期限，重复取签名不延长旧权限。

分片回执包含completed、unused、unknown三种结果。completed必须用实际ListParts的大小和ETag核对；unused是受控浏览器对“从未启动该权限PUT”的声明，存储服务不能独立证明这个否定事实，因此该协议依赖官方客户端遵守一次权限一次发送。自制客户端不得在声明unused后再用旧URL发送。超时、abort或刷新丢失结果只能是unknown，不能改成unused。浏览器取消会停止新任务、等待已启动请求收尾并提交回执；服务端仍须等待权限到期、exact Abort/ListParts/HEAD关闭证据后才能释放未完成对象的预算。单独ACK、过期或一次HEAD404均不释放预算；真正无法判定的PUT继续保留空间并进入运维核查。

不得通过回滚数据库、把状态改为stored、归零预算、清空未知用途或删除素材主记录来“恢复”远端已删除字节。缺失的原件仍是缺失；已验证平台映射继续保存。数据库迁移回退仅用于有计划的数据兼容操作，不是对象存储恢复机制。任何人工解除阻塞都必须关联本租户/BC/代次的可审查证据。
