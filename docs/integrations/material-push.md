# 素材工具 → TK-ADA：批量推送接口

本接口把素材工具已有的 R2 视频登记到对应租户素材库，异步校验并上传至默认 BC 的主素材账户。不复制到 TK-ADA 的 R2，不创建广告。文件名原样保存和提交 TikTok，不加后缀。

## 1. 请求参数

`POST /api/integrations/materials/batches`，`Content-Type: application/json`。

```json
{
  "tenant_name": "骏伯",
  "materials": [
    {
      "material_id": "tool-video-20260929-001",
      "file_name": "原始素材名称.mp4",
      "url": "https://materials.example.com/videos/001.mp4"
    },
    {
      "material_id": "tool-video-20260929-002",
      "file_name": "第二条素材.mp4",
      "url": "https://materials.example.com/videos/002.mp4"
    }
  ]
}
```

| 字段 | 必填 | 规则 |
| --- | --- | --- |
| `tenant_name` | 是 | 数据库中的完整租户名称，精确匹配，1–120 字符；租户必须已启用。不是 UUID，不自动创建租户 |
| `materials` | 是 | 非空数组，1–200 条；一次只属于一个租户，无另设单素材接口 |
| `materials[].material_id` | 是 | 素材工具唯一 ID，1–255 字符；同租户内识别更新，批次内不能重复 |
| `materials[].file_name` | 是 | 完整原始文件名，含扩展名，1–100 字符。不能带路径、控制字符或首尾空白；不合规则拒绝，不裁剪、不改名 |
| `materials[].url` | 是 | 已上传原件的公网 HTTPS URL，最多 8192 字符；不需要配置来源域名白名单 |

不接受额外字段。请求体最多 2 MiB。当前支持视频扩展名 `.mp4`、`.mov`、`.m4v`、`.avi`、`.webm`、`.mpeg`、`.3gp`、`.mkv`；后台核实文件可识别且容器与扩展名相符，实际平台接受能力仍由既有上传通道决定。

无需提交文件大小、MIME、时长、宽高、SHA-256、MD5、校验时间/来源、存储状态/版本、创建时间。后台读取原件计算摘要和大小，运行 ffprobe 读取视频信息，并记录系统字段。读取时会有短期本地临时文件，结束即清理，不会产生第二份 R2 原件。

## 2. 验签与接入配置

运营人员无需配置凭证。系统管理员在目标环境的私有配置中维护 `MATERIAL_PUSH_CLIENTS`，由素材工具服务端保存对应密钥。有效凭证允许按名称推送到所有有效租户（包括后续新建租户），无需配置租户名单或 UUID 映射。该凭证权限较大，仅交给受信任的服务端 Agent。

配置结构示例（占位符不能直接用于运行）：

```json
{
  "material-tool": {
    "secret": "替换成安全生成的至少32字符随机密钥",
    "actor_id": "TK-ADA专用执行用户UUID"
  }
}
```

`actor_id` 是 TK-ADA 内部审计执行身份，不是素材工具的请求参数。部署时配置独立、非平台管理员的启用用户并使用不可知随机登录密码。系统按需建立租户 `operator` 成员，无需人工逐租户配置；已有停用或只读成员不会被自动提权/重新启用。租户名称必须精确匹配唯一的有效数据库租户；不会自动创建租户。去掉域名限制不等于允许内网访问：仍强制 HTTPS、公网 DNS/IP、固定连接 IP、TLS 校验、禁止重定向及大小/时限校验。

API、所有 Worker、Beat 必须加载相同配置及连接加密密钥；`MATERIAL_PUSH_CLIENTS={}` 不开放任何接入方。不要把密钥放前端、普通表单或 Git。移除 key 会阻止该 key 的新请求、尚未完成的原件读取及后续依赖原件的操作，不会删除已上传的 TikTok 素材。保留 key ID、原地更换 secret 可轮换凭证；在发送端同步更新。不要将旧 key ID 分配给另一个接入方。

四个必填请求头：

| 请求头 | 值 |
| --- | --- |
| `X-Key-Id` | 管理员分配的标识，如 `material-tool`；字母、数字、下划线、横线，最多 128 字符 |
| `X-Timestamp` | 当前 Unix 秒，10 位数字；服务器允许 ±300 秒 |
| `X-Request-Id` | 标准小写 UUID；同批请求重试必须保持不变 |
| `X-Signature` | `sha256=` 加 HMAC-SHA256 小写十六进制摘要 |

签名消息按以下五项用 `\n` 连接，末尾没有换行，整体 UTF-8 编码：

```text
POST
/api/integrations/materials/batches
<X-Timestamp>
<X-Request-Id>
<SHA256(实际发送的原始请求体字节)小写十六进制>
```

使用 secret 的 UTF-8 字节作为 HMAC 密钥。不要使用 JSON 对象重序列化后的摘要代替实际请求体摘要。接口不接受查询参数；签名路径不含域名，反向代理应保留 `/api/...` 路径。查询批次也需要签名，方法改为 `GET`，路径包含实际批次 ID，body 为零字节。

### Python 签名示例

下面函数不会自行发出请求。发送端将返回的 `body` 原样发送；不要再用 HTTP 客户端的 `json=` 重新序列化。

```python
import hashlib
import hmac
import json
import os
import time
import uuid


def signed_request(method, path, body, request_id):
    timestamp = str(int(time.time()))
    message = "\n".join([
        method, path, timestamp, request_id,
        hashlib.sha256(body).hexdigest(),
    ]).encode("utf-8")
    signature = hmac.new(
        os.environ["TKADA_PUSH_SECRET"].encode("utf-8"),
        message, hashlib.sha256,
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Key-Id": os.environ["TKADA_PUSH_KEY_ID"],
        "X-Timestamp": timestamp,
        "X-Request-Id": request_id,
        "X-Signature": "sha256=" + signature,
    }


payload = {
    "tenant_name": "骏伯",
    "materials": [{
        "material_id": "tool-video-001",
        "file_name": "原名.mp4",
        "url": "https://materials.example.com/videos/001.mp4",
    }],
}
body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
request_id = str(uuid.uuid4())  # 在素材工具持久化该 ID 和 body，供重试使用
path = "/api/integrations/materials/batches"
headers = signed_request("POST", path, body, request_id)
# 发送 POST，HTTP body=body，headers=headers。
# 网络超时：保留 request_id 和 body，仅重新计算 timestamp/signature。
# 查询：signed_request("GET", path + "/" + batch_id, b"", str(uuid.uuid4()))
```

## 3. 默认 BC 与上传位置

平台管理员：**平台管理 → 租户 → 编辑 → 默认 BC**，可选择或清除。未设置时按现有 BC 目录中 `bc_id` 升序的第一条选择，不是随机选择。所选 BC 已失效或没有可用连接/上传账户时报错，不悄悄换到其他 BC。

BC 内继续使用已有默认连接和主素材账户选择规则。BC 默认连接与租户默认 BC 是两个层次，无需在推送请求重复指定。批次接收后冻结租户、BC、连接及授权代数；修改默认值不会将旧批次改投其他 BC。接口只上传素材，不创建或启用广告，也不把素材自动推到所有广告账户。

## 4. 返回值与进度查询

HTTP `202` 仅表示批次已完整登记并入队，不等于 TikTok 上传成功。参数/权限检查失败时整批不登记；网络读取/文件校验/平台上传是逐项执行，一项失败不撤销其他项。

```json
{
  "batch_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "request_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  "tenant_name": "骏伯",
  "bc_id": "实际选中的BC",
  "status": "accepted",
  "accepted_count": 1,
  "created_at": "2026-09-29T00:00:00Z",
  "materials": [{
    "material_id": "tool-video-001",
    "revision": 1,
    "file_name": "原名.mp4",
    "library_material_id": null,
    "status": "queued",
    "error_code": null,
    "advertiser_id": null,
    "video_id": null
  }]
}
```

带相同接入方验签调用 `GET /api/integrations/materials/batches/{batch_id}`。建议每 3–10 秒查询一次，逐步退避；数组按 `material_id` 排序，不保证原始提交顺序，按 ID 对应。

批次状态：`accepted` 等待校验；`processing` 处理中或结果核实中；`completed` 全部可用；`partial_failed` 部分可用、其余失败/阻塞；`failed` 全部失败/阻塞。

单项沿用素材导入状态：`queued`、`validating`、`stored`、`uploading`、`verifying`、`available`、`result_unknown`、`blocked`、`failed` 等。只有 `available` 才代表有平台可用库存，`advertiser_id`/`video_id` 是实际已记录结果；不要从 HTTP 202 推断上传成功。`result_unknown` 表示平台发送结果待核实，不能当作明确失败反复提交新上传。

## 5. 重试、更新和原件生命周期

- HTTP 超时/响应丢失：同 `X-Key-Id`、同 `X-Request-Id`、完全相同 body 字节重试，重新计算时间戳与签名。持久幂等返回原批次及最新进度，不创建新修订。相同 request ID、不同 body 返回 409。
- 要更新内容、文件名或续期 URL：用新的 request ID，同租户同 `material_id` 推送。外部素材业务记录的修订递增；目录只展示当前有效内部版本。最新修订校验失败时仍保留之前版本，不丢失素材。
- 相同 ID、名称、内容和 BC 可复用已可用版本；同连接的未完成上传也沿用原任务，包括未知结果，不能绕过核实产生重复上传。新 URL 只有读回校验为相同内容后才替换原件引用。
- 内容或名称变化创建新的内部素材版本；旧版本与既有广告引用、VID 保留不改。晚完成的旧任务不能覆盖新版本。
- 原件由素材工具管理。必须使用稳定、不可覆盖的对象 URL，更新内容时换对象路径；TK-ADA 不能保证外部工具在校验后偷偷覆盖 URL 的内容不变。已校验不意味着接管原件保管。
- URL 必须能由 TK-ADA Worker 和 TikTok 直接访问，不依赖浏览器 Cookie、自定义请求头、登录页或跳转。不限定来源域名，但拒绝私网/回环/元数据地址以及跳转。
- 优先使用长期可读 URL。临时签名链接必须覆盖排队、校验、平台拉取及恢复时间，不能只在接收时有效。过期导致失败时续期 URL 后用新 request ID 重推。
- TK-ADA 不删除该外部对象、不发起其分片写入，也不占自有 R2 临时对象预算。保留原件可支持后续原件预览和依赖 URL 的再次分发；如果素材工具删除原件，这些能力可能失效，但已经记录的 TikTok 库存不因此被删除。

## 6. 常见错误

| HTTP / 单项码 | 含义 / 处理 |
| --- | --- |
| 401 `push_unauthorized` | 密钥、签名、时间戳或请求标识无效；核对时钟及实际发送字节 |
| 403 `push_forbidden` / 租户权限码 | 执行用户已停用或其既有租户成员权限被撤销；由管理员核查 |
| 422 `push_invalid` / `push_tenant_invalid` | 参数错误或不存在/停用租户；修正后新请求 ID 提交 |
| 422 `push_url_invalid` | URL 不满足 HTTPS、主机、端口等限制 |
| 413 `push_too_large` | body 超过 2 MiB，拆批 |
| 409 `idempotency_conflict` | request ID 已用于不同字节内容 |
| 409 `push_bc_missing` / 连接权限码 | 没有 BC、默认连接或合法上传目标 |
| 单项 `push_url_unreachable` | DNS/网络/HTTP/链接有效期问题，或返回跳转 |
| 单项 `push_file_too_large` / `push_file_incomplete` | 超容量或读取不完整 |
| 单项 `invalid_video` | 无有效视频轨道/时长/尺寸，或容器与扩展名不一致 |
| 单项 `push_prior_upload_pending` | 已有同素材任务尚未完成且冻结连接不同；先处理原任务 |
| 单项 `push_worker_interrupted` | 多次处理进程中断，需排查资源、超时与 Worker 后再重推 |

单个视频上限沿用 `MATERIAL_URL_MAX_UPLOAD_BYTES`，校验时限沿用 `MATERIAL_VALIDATION_SECONDS`。未知平台结果沿用现有上传账本恢复，不通过删除账本或改名重试。

## 7. 发布前检查

1. 按部署手册备份并排空任务；迁移到 `material_push`。迁移遇到重名租户会明确停止，须管理员先消除歧义，不自动重命名。已有推送批次或默认 BC 配置时禁止直接降级丢弃数据。
2. 配置有效系统密钥、专用执行用户、默认 BC/连接和主素材账户；无需租户或来源域名名单。核对 `MATERIAL_INGEST_ENABLED` 的授权值。外部原件不要求写入自己的 R2，但后续封面等既有能力仍可能需要现有对象存储配置。
3. 核实 API/全部 Worker/Beat 配置一致。resources Worker 必须是有硬时限的 prefork 模式并安装 ffprobe；Beat 每 60 秒修复中断的外部校验任务。确认磁盘临时空间可容纳并发校验原件。
4. 在用户明确授权的真实租户、BC 和测试素材范围做签名推送，回读平台 VID/文件名及素材库状态。不得把本地 HTTP 替身测试或 202 响应当作真实联调通过。

本次实现只在本地验证，尚未部署、配置真实接入密钥或执行真实 R2/TikTok 联调。
