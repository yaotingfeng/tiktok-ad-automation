# 搭建链路诊断

操作记录保存业务结论；运行日志保存队列等待、物理调用、解析与回执保存的过程。Celery `SUCCESS` 只表示本次函数返回，不等于广告创建成功。以 PostgreSQL 业务状态/平台回执为准。

## 采集可留存文件

使用已有集群读取权限，从仓库根目录执行：

```bash
python3 scripts/collect-build-diagnostics.py \
  --kubectl /path/to/kubectl --kubeconfig /private/path/kubeconfig \
  --namespace test --tenant <租户UUID> --submission <提交UUID> \
  --output .runtime/build-diagnostics/<批次> --since 10m
```

只读数据库事务、每查询5秒上限，不修改线上任务。输出目录必须被Git忽略；新建目录0700、文件0600。保存提交/步骤/封面/共享批次、投递与执行队列快照，builds/resource/results/control日志，以及Pod UID、镜像、创建时间、重启次数与上次退出原因。存在前一容器时同时采集previous日志。每次采集使用独立时间戳和清单，不覆盖前次证据。

这是按需采集工具，不是已部署的永久集中日志服务；Pod删除或日志轮转后，不能保证追溯旧日志。持续排查时每分钟采集一份，发布前先采集旧Pod。需要全天保留时，应由部署侧日志采集器持续保存stdout，并设置访问权限及保留期限，不能把容器内临时文件当永久档案。

## 发布后的结构化日志

`runtime_trace`后为JSON，可按tenant_id、task_id、step_id、job_id、batch_id关联；SDK线程显式继承父任务身份。包含时间和PID，不输出请求正文、令牌、素材URL或异常原始正文。

| 事件 | 证据含义 |
| --- | --- |
| task_started/task_finished | 发布→开始、到期→开始、执行耗时；Celery状态不代表业务成功 |
| request_scope_started/finished/failed | 包括业务授权、额度准入和SDK处理的总耗时 |
| sdk_http_started/response/failed | 已进入SDK HTTP请求函数、HTTP响应状态、耗时、平台request_id/数字错误码、已返回对象ID；started不证明请求到达平台 |
| ad_dependency_wait | 当前广告具体缺失的素材、封面job/batch、状态与错误码 |
| build_local_transition | 未发送阶段的业务转移意图、attempt和下次重查间隔；事务仍可能失败 |
| build_receipt/cover_receipt及fallback | 收到ID后的主保存、备用保存各自开始/完成/失败 |
| cover_failed/cover_share_failed/build_armed_error | 原始异常类型、代码位置、SQLSTATE或底层异常类型，保留平台关联证据 |
| cover_share_not_sent | 本调用明确未发送；旧批次保留，当前成员已交回正常准备 |
| cover_share_slice | 切片阶段、核验次数/耗时、找到数量、总耗时和结果 |

排查顺序：先看业务状态及最后进展时间，再看未发布消息/队列等待，再按同一任务查request_scope和sdk_http，最后查回执保存。收到响应与保存完成是不同证据；缺少response事件不能直接推断网络断开，也可能是进程被终止，应结合PID、Pod退出状态和Celery父进程日志。

快照`create_interval`是成功步骤REQUEST_ARMED到CREATED的间隔，包含准入、平台调用及数据库保存，不能标成纯HTTP耗时。`tenant_due_outbox`覆盖该租户全部业务，`deployment_queue_depths`是整个部署的队列，不应全部归因于单个提交。

## 恢复边界

只有当前调用返回明确NOT_SENT或本地准入拒绝，且本次刚armed、全部成员租约仍归当前执行器、无已知ID/候选及批次回执时，才允许恢复发送。旧armed批次保持不可变，以BLOCKED/cover_share_not_sent留档，成员重新规划。历史UNKNOWN不能只凭error_code反推没有发送，禁止清除armed或盲重发。

广告等待素材/封面的本地重查为15/30/60秒，最高60秒；不改变平台额度、并发及UNKNOWN的只读恢复语义。相较固定15秒，长期等待的检查频次最多减少75%，但依赖刚恢复后的发现可能多等最多约一分钟。
