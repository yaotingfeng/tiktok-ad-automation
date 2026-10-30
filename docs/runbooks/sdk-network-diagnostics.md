# 官方SDK网络故障诊断

本方案为应用诊断代码，当前发布状态以实施进度和当日验收记录为准。只观察现有官方SDK请求，不新增探测请求、不修改SDK重试或原任务授权范围。

## 请求日志与含义

原有`request_scope_started/finished/failed`继续覆盖本地授权、准入与调用范围。原有`sdk_http_started`及`sdk_http_response/failed`增加本地`http_attempt_id`和`transport`摘要；每次仍只有原来的开始/结束日志，不逐阶段追加日志行。SDK线程显式继承任务、租户、BC、账户和步骤关联身份。

摘要包括阶段耗时、失败阶段、连接/读取超时参数、对端IP及端口、TLS版本、连接复用情况、客户端写入与完整响应标志、原异常类型/代码位置。失败时另附Pod、构建版本、urllib3版本和诊断源文件指纹。构建版本来自镜像内`TK_ADA_BUILD_REVISION`，由既有Docker构建参数`VCS_REF`烘入；它不是业务开关，也不改变运行配置的四个布尔开关。

阶段必须按观察边界解释：

| 字段 | 实际含义 |
| --- | --- |
| `dns_tcp_connect` | SDK `_new_conn`的DNS解析与TCP建立合计，不能从它单独断言DNS或TCP哪一步慢 |
| `connection_setup` | 整次连接准备总耗时 |
| `tls_setup` | 总连接耗时减去DNS/TCP及可见CONNECT隧道耗时，包含证书校验等准备，不是精确的纯TLS握手计时 |
| `proxy_tunnel` | 配置代理时的CONNECT隧道阶段 |
| `request_write` | SDK连接的`request()`调用范围 |
| `response_read` | `getresponse()`调用范围，包含响应等待与读取，不能当成独立首字节延迟 |

`send_state=not_sent`必须来自当前客户端、当前nonce在HTTP请求前捕获的失败；普通异常名字不能证明未发送。`may_have_sent`表示请求入口已经开始、可能部分写入；`write_completed`只表示客户端写入调用完成，不证明平台已创建。诊断摘要本身不参与自动重发授权判断，原SDK错误分类和持久化尝试证明仍独立生效。

## 关键创建失败的持久证据

SDK广告创建失败的安全摘要随原有`NOT_SENT`、`RESULT_UNKNOWN`或`REMOTE_REJECTED`写入`StepEvidence.summary.transport`，不新增证据行、不改原attempt/nonce、正文摘要和历史记录。进程退出或Pod重建后仍可从数据库按原任务查询；不把stdout保留当作证据持久化前提。

仅管理员在已选定的租户和步骤范围内使用只读查询，禁止全租户JSON扫描：

```sql
BEGIN READ ONLY;
SET LOCAL statement_timeout = '5s';
SELECT observed_at, conclusion, attempt, request_id, summary->'transport' AS transport
FROM step_evidence
WHERE tenant_id = :tenant_id
  AND submission_id = :submission_id
  AND step_id = :step_id
ORDER BY observed_at DESC
LIMIT 50;
ROLLBACK;
```

stdout中的本地HTTP尝试号、数据库中的attempt/nonce和TikTok `request_id`属于不同关联身份，不得互相代替。没有平台响应时不能编造TikTok请求号。

## 留存与安全边界

当前ACK已安装LoongCollector，但只读审计仅确认一条ingress采集声明，未确认TK-ADA已接入SLS集中留存；云端额外规则及Logstore TTL尚未核验。Kubernetes容器日志不能作为跨Pod生命周期的完整归档，见[官方节点日志说明](https://kubernetes.io/docs/concepts/cluster-administration/logging/#how-nodes-handle-container-logs)。不得把已安装采集器写成应用日志已完整留存。

本补丁的持久保障限于已有任务证据中的关键创建失败摘要；连续运行日志仍需单独核验TK-ADA的采集范围、目标Logstore、保留期限、访问权限和实际检索结果。未创建云日志资源、未改变计费或集群采集配置。

摘要使用固定白名单、固定阶段集合及字符串/数值上限；最多8个代码位置，每个不超过220字符。只允许合法IP，拒绝IPv6任意scope文本。不记录请求URL、查询参数、请求/响应正文、访问令牌、Cookie或签名链接；平台原始错误message也不进入创建错误日志。诊断自身异常不能覆盖SDK成功回执或原网络异常；进程中断仍正常传播。

调用返回或抛异常时才能生成结束摘要。SIGKILL、节点宕机或进程在记录前消失，可能只留下开始日志和原有租约恢复证据；不能据此承诺重建完整网络过程，也不能仅凭应用阶段日志归责到具体网络跳。
