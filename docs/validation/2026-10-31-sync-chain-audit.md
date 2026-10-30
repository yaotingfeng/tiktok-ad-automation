# 2026-10-31：大数据同步全链路审计与历史复盘

## 范围和证据边界

用户要求回顾历次修改、沉淀运维经验，并检查调度、请求数、接口频率和并发还有哪些提升空间。本轮只读生产、审阅 Git/源码并编写文档，没有修改生产限额、清理数据、发布或调用 TikTok 写接口。

- 冻结源码：`78f92247da36aac3726e2e44f0ee549983a08d14`；应用代码与此前已发布的 `0642ad3d7d57115df1a61464b0aec2c1452c9fdb` 一致，后续为文档。基线迁移为 `0067_merge_directory_turn`。
- 在现有独立工作树 `.worktrees/sync-throughput` 编写；主工作区同时存在未提交的统一配额/批量管理改造，**不纳入已发布能力**。
- 生产采样为北京时间 10/31 00:28–00:41（UTC 10/30 16:28–16:41）。数据库查询使用只读事务、4 秒 statement timeout、300 ms lock timeout。
- 原始日志、实际配置探针、数据库元数据只存本机 `.runtime/sync-chain-audit-20261031/`；不提交账户 ID、凭据和原始业务报表。本文只保留脱敏汇总。
- [长期运维参考](../runbooks/sync-performance.md)承载口径、链路、优化优先级和验收流程；本页记录本次事实与历史结论。

## 一、结论

1. 已有优化是真的，但先前多次只验证一个局部，没有证明整个系统持续“消化量大于新增量”。历史中确有错误修复和回滚，也有正确性缺陷使调参无法解决的问题。
2. 生产**不是所有 API 共用 3 QPS**。主要报表/目录已是物理接口 10 QPS；未覆盖入口仍受默认 3 QPS 和共享兜底 app 10 QPS。覆盖不完整值得修，但不是把现有全部吞吐解释成 3 QPS。
3. “每分钟 30 个任务、12 个素材任务”来自短窗口固定历史批次，不是全系统每分钟处理的数据行。新窗口三个 Worker 角色约 19.2 次真实 API 尝试/秒，确认提交的广告/素材事实写入约 1142 行/秒；这是重写也计入的写入吞吐，非净新增。
4. 平台限制确实存在：同窗口 108 次 `40100` 均来自素材拆分接口；本地 endpoint_rate 拒绝 2127 次。继续盲目加并发会放大争抢，应把物理配额、等待唤醒与重复请求一起治理。
5. 数据库已经千万级。大头包括目录身份、素材引用、暂存和控制流水，不能只看 193 万行的 `report_sync_job`。有批量写和索引不等于长期增长已经受控。

## 二、Git 历史：修改了什么，为什么没能一次解决

可复现扫描：对冻结 main 历史执行 `git log --full-history --no-merges --date=short`，限定以下路径：

```text
backend/app/modules/reporting
backend/app/modules/ads/sync.py
backend/app/modules/ads/sync_models.py
backend/app/modules/ads/directory*.py
backend/app/modules/ads/tasks.py
backend/app/modules/ads/material_directory_children.py
backend/app/modules/accounts/runtime_directory.py
backend/app/modules/accounts/runtime_directory_tasks.py
backend/app/jobs
backend/app/integrations/tiktok/admission.py
deploy/k8s/tk-ada-workloads.yaml
```

匹配 **305 个非合并提交**，日期 2026-09-09 至 10-31；10/15 起 156 个、10/27 起 65 个。按 patch-id 为 304 类，`5c91a838` / `cfc7baff` 为相同补丁。完整索引见[提交清单 CSV](2026-10-31-sync-commit-inventory.csv)。这不是“305 次失败优化”：含新功能、共享基础代码、回滚和补丁移植，也不穷尽仅改 adapter、迁移或文档的提交；另阅读相关验收记录和调用方补足上下文。

| 阶段 / 代表提交 | 解决的问题和已有证据 | 未覆盖的边界 / 教训 |
| --- | --- | --- |
| 9/30–10/12 `64db52d0`、`40560e58`、`71a9164b` | 冻结路由、分页、版本与可恢复任务基础 | 正确执行基础不等于持续吞吐有保障 |
| 10/14–15 `017643e7`、`289125dd` | 活跃/广告类型筛选，空账户与重复工作缩减 | 提交中“90%”描述不是全天生产结果；活跃筛选也不能替代迟到修正 |
| 10/16–18 `c909ac56`、`75dcc8d9`、`008c63d2`、`c80421c3` | 批量与原子发布、读/发布隔离、实时/历史拆分 | 多逻辑 lane 仍可能争相同物理进程 |
| 10/19 `99ff9a5a`、`ec42de03` | 发现 2308 补链任务对应 459 scope，2302/2303 到期未租；历史事实 30 分钟无完成 | 95.4% 已完成补链不发物理请求：任务数不能换算 API 数或有效数据量 |
| 10/19–20 `0699e42b`、`a4fdc9ae`、`1e444602`、`e78f282d` | 同期候选、查询缓存、精确身份、证据唤醒；确定性场景请求 180→7 | 场景改善不能外推全量；275 个零消耗来源缺口没有伪造为成功 |
| 10/20 `bb4a9715` | 修 generic plan 问题，8 轮 16 次查询最长 0.465 秒，custom 11/generic 0 | 热缓存/单 SQL 通过，仍需验证完整调度事务和冷数据 |
| 10/20–21 `fac30065`、`bb92ac5e`、`95c34bf2`、`53a5888f` | 热数据 15 分钟、目录 30/60 分钟、取消目录触发财务重拉 | 未形成稳定周期的总请求降幅与新鲜度 P95 证据 |
| 10/27 `092cce49`、`8c636d9d`、`c68f9a06` | 优先级、窗口、候选、outbox、清理；恢复 31 ms、4859 账户筛选 127 ms | 后续积压仍上升，局部查询变快不代表净流入转负 |
| 10/27 `4966a8ac`、`77226c4d`、`7fb7293b`、`40837493` | 长发布、子页、续点、补位；真实 125.764 秒发布提交，3 大任务 COMPLETE，control 4717→42 | 控制队列清掉不等于业务历史清完；长事务不自动等于全程锁阻塞 |
| 10/27–29 `e2b49f5f`、`70aa7a1a`、`33354ae4`、`7612c338` | 分页漂移、补链维护、续步与冗余目录修复；样本恢复、来源恢复 0.042–0.186 秒 | 历史 WAITING 仍需按原因和游标进展细分 |
| 10/30 `ce585f03`、`811da3f1`、`4101d13e`、`26f911b4` | planner 前缀持久化、健康超时；候选访问 210000→30080 | 仍随活跃集合增长，随后 3×15 分钟积压净增长 |
| 10/30 `099fe34a`、`3b38fda4`、`449375dd`、`ee7ba6bf`、`48123e1c` | 部分索引/generic plan、聚合、收尾索引；control 2→3、backfill 3→4 | 实际组合 SQL 2.504 秒，不能用较早热查询 219 ms 代替 |
| 10/30 `b95fcccd`、`cd15fd33` | 目录缺口不再重拉新鲜报表、同状态公平性；40 旧+3 缺失 PG 回归 | 目录覆盖改善不等于报表、补链、历史全部收尾 |
| 10/30 `1e99743f` | **轮播补链错误要求视频 VID**；4859 账户审计的35551 个未关联素材中35523 个为轮播；按类型修正完成条件 | 99.92% 是未关联素材构成，不是 99.92% 请求无效；业务完成条件错误，升配无法治愈 |
| 10/31 `0642ad3d` | 历史事实收回后台目录/补链借用容量，页续发归还机会；素材历史完成约 2.7 倍 | 这是容量分配改善，没有增加物理进程、减少总请求或证明积压清空 |

### 明确纠正过的误判

- `c17bb531` 把原 publisher 的 `ads-publish` 改为 `ads-directory` 并增并发，会使发布队列失去原消费者；`e0b85952` 已完整回滚。不能据此宣称“找到了错误队列根因、8 倍提速”；Git 本身也不证明错误版本曾上线。
- “report_sync_job 182 万行，所以任何查询都超时”缺少计划与范围，不能成立。行数、查询参数、索引和 generic plan 必须一起看。
- 136 秒事务不等于 136 秒阻塞锁；既有三个样本没发现 blocking PID。也不能反向推断从未有锁问题。
- 24421 个后台目录运行没有发现按冻结分区的重复，不能直接整批当垃圾删除。
- `claim_generation >= 100`、`skip_locked` 后 not_found 等为待构造复现的恢复风险，未证明是当前主要生产根因，不写成已定位故障。

反复修复的核心教训是：混合了正确性、调度公平、查询计划、容量和平台等待，且完成标准不统一。应分别解决，但每一轮都必须回到同一组端到端指标验收，不能看到局部改善就宣布全局完成。

## 三、实际配额与最新吞吐

### 3.1 策略证据

00:31:43 的运行设置读取自生产 API；策略 SHA-256：`09925e05f7c5a2bccae041387bd51a24d28ae8c670c066096c875f26cbb850fb`。本次未重做 11 角色逐一策略探针，上一发布记录已做角色一致性验收。

| 项 | 当前值 / 语义 |
| --- | --- |
| base app / endpoint | 10 / 3 次每 1000 ms；未映射操作共用 app fallback |
| base app / tenant / advertiser inflight | 32 / 32 / 4 |
| base endpoint inflight / lease | 3 / 960000 ms |
| OFFICIAL_API 已覆盖读取 | 物理接口 rate_group + endpoint 两层均有效限制为 10 QPS |
| ads.get_ads、reports.material_breakdown | endpoint inflight 12 |
| 其余多数已覆盖读取 | endpoint inflight 6 |
| daily call budget 参数 | 0；不等于 TikTok 平台不设 QPD |

覆盖读取包括 reports.integrated/bc/material_overview/material_breakdown、ads.get_ads/get_campaigns、build.get_ads/get_campaigns/get_adgroups/get_regular_adgroups、materials.get_videos/search_videos、scene.get_identity_video。music.get 虽有物理映射但没有 endpoint 覆盖，仍会遇到默认 3 QPS。

同一逻辑操作的 OFFICIAL_API 覆盖优先于非通道覆盖；例如素材拆分非通道值 5 QPS 不能被误报成当前 SDK 实际 5。其余未映射入口、bootstrap 和 MCP 要单独审计。

官方额度等级和例外见[官方频率文档](https://business-api.tiktok.com/gateway/docs/index?doc_id=1740029171730433&identify_key=c0138ffadd90a955c1f0670a56fe348d1d40680b3c89461e09f78ed26785164b)。当前 app 具体等级尚未核验；本地默认值不是平台授权额度证据。

### 3.2 约 5 分钟生产日志

三个 deployment 各抓取一份 `--since=5m` 日志，事件覆盖约 00:28:42–00:33:43。各窗口相差不到 1 秒；不是全角色平台用量审计，不保证覆盖其他副本。以下按观测事件计算，不外推全系统 SLA。

| Worker 角色 | HTTP 开始 / 响应 | 平台 40100 | COMPLETE 广告日报写入行 | COMPLETE 素材日报写入行 |
| --- | ---: | ---: | ---: | ---: |
| ads-backfill | 715 / 714 | 17 | 174938 | 5450 |
| ads-reporting | 3127 / 3129 | 91 | 31930 | 130964 |
| ads-live-directory | 1934 / 1934 | 0 | 不适用 | 不适用 |
| 合计 | 5776 / 5777 | 108 | 206868 | 136414 |

- HTTP 尝试约 19.2 次/秒；跨多个物理 endpoint，不能拿它和单 endpoint 10 QPS 比较。`sdk_http_started` 在调用底层 request 前记录，连接失败时未必到达平台，不等于平台接收次数。
- 343282 行只统计 `job_commit_confirmed=true` 且 `job_commit_result=COMPLETE` 的 `counts.rows`，约 1142 行/秒；不是净新增、不是目录对象行数。account_day 没有该行数指标，不把缺失解释为写了 0 行。
- backfill 完成 account/ad/material 107/117/123 个不同任务；reporting 完成 ad/material 261/1412 个不同任务。批次、账户数、日期跨度和页数不同，不能拿该窗口与前一个固定历史批次直接做提速比。
- 108 次 40100 全部来自 `reports.material_breakdown`，该接口观测响应 2760 次，约 3.9% 被平台限流；不能判断都是 QPS，仍需平台响应中频率类型/Retry-After 分类。
- 本地观测 endpoint_rate 2127 次、endpoint_inflight 9 次。被本地拒绝没有发 TikTok HTTP，仍占用领取/检查/回写/补位成本。

| 角色 / 接口 | 响应数 | P50 / P95 毫秒 |
| --- | ---: | ---: |
| backfill / reports.integrated | 354 | 789 / 1556 |
| reporting / reports.material_breakdown | 2521 | 328 / 514 |
| reporting / reports.material_overview | 347 | 1116 / 1360 |
| live-directory / ads.get_ads | 630 | 1961 / 3869 |
| live-directory / build.get_ads | 449 | 691 / 1329 |

日志为客户端调用总耗时，不是平台服务端纯计算时间。广告目录读取，尤其 `ads.get_ads`（`/ad/get/`，也用于创意读取），比素材拆分更占槽；按物理接口设置并发比统一一个数合理。

reporting 的 inclusive read 为 2390.38 槽秒，refill 为195.81 槽秒；backfill read 690.79、financial_publish 135.92、refill 31.26 槽秒。不能把父子阶段累加。只看 facts INSERT 会漏掉状态/汇总成本：后台 publish_facts 28.29 秒，publish_state 59.90 秒。依据这些阶段可优先调查重复补位和批量状态更新，但还不能声称它们是全部瓶颈。

### 3.3 前一次容量修正的可比结果

[发布记录](2026-10-30-background-fact-capacity.md)固定 12393 个历史事实任务：发布前7.19分钟完成20.16个/分钟，发布后4.79分钟35.88个/分钟，约1.8倍；其中素材日报4.73→12.72个/分钟，约2.7倍。原批次仍有11772未完成。采样分批读取不是瞬时事务快照，短窗口不能承诺全天持续速度或清空时间。

前一次 335 秒后台日志中，4 次余额步骤用了约149槽秒，说明“目录/补链各一候补”不是事实获得两个独占物理槽位。旧补链40样本7分50秒内有5次请求计数推进、4次游标推进、0完成，不能仅凭 WAITING 就认定完全无效。

## 四、数据库已经达到千万级

00:31:44 从 `pg_class.reltuples` 和 relation size 读取，非全表精确 COUNT。数据库 119039754767 字节，约110.86 GiB；表大小包含其索引/TOAST，分区事实汇总父子 relation。未查询云端分配上限，不能称磁盘即将满。

| 表 | 估算行数 | 总大小 GiB |
| --- | ---: | ---: |
| ad_object | 26928192 | 42.39 |
| ad_material_reference | 35036680 | 21.49 |
| report_sync_page | 2148677 | 12.58 |
| pending_dispatch | 14585777 | 8.13 |
| ad_directory_page | 217869 | 6.86 |
| ad_directory_run | 2444246 | 4.22 |
| material_report_day（23 relations） | 7480000 | 4.14 |
| ad_report_day（23 relations） | 6056977 | 4.12 |
| report_sync_job | 1935614 | 2.12 |

目录对象同主键 UPSERT；素材引用同使用身份 UPSERT。没有证据证明这些行是重复版本快照。历史素材身份用于消耗归属，当前选择是按广告查询 complete 身份后在 Python 取最新；不能用全表最大 version 删除旧身份。

冻结源码未发现广告目录/素材引用的物理历史回收，也未发现终结目录运行元记录和同步 outbox 的通用回收；`outbox.py` 当前压缩仅处理 builds 两类消息且保留幂等身份。目录 superseded 清理是置 STALE，不是删除元记录；目录页清理是删除暂存正文。这解释了长期增长风险，但不能量化每日净增长——需要多个时点同口径采样。

00:41 的 `pg_stat_user_tables` 显示近期 autovacuum 在运行：ad_object dead/live 约43.7万/2693.6万、引用约22.5万/3504.1万，均有当小时 vacuum；pending_dispatch 约16.1万/1471.8万。它们是另一个统计视图/时点，不能与上表相减当作净增长；也不能从表大推断严重 MVCC 膨胀。当前没有安装 pg_stat_statements，本轮未安装任何扩展。

应追加的定向调查：单广告素材身份长尾、当前选择后的历史占比；outbox/运行元数据每日净增长；暂存按父任务状态/年龄/字节分布；代表性 SQL 的扫描放大、分区裁剪、冷缓存、WAL/IO 和内存。现有证据不足以判断当前 DB CPU 或内存是否已饱和，不能据此直接要求再次升配。

## 五、下一轮实施顺序及验收

详细动作与验收表见[运维参考第四节](../runbooks/sync-performance.md#4-减少请求与调度成本的优先路线)。建议先形成同口径基线，再逐项实施：

1. **配额完整映射和有效等待**：已知素材拆分在10QPS附近仍有40100；先核实真实app等级/各时间窗口，补齐其他入口，不把“全局50”当答案。同时减少限流后重复领取和全局refill。
2. **减少生成和重复读取**：按近期变化/历史修正分层；复用完整查询结果和创建回执。保留归因修正、重新活跃账户和周期兜底。
3. **调度与慢接口容量**：让候选查询随本轮实际空位增长；测余额任务跨HTTP持事务；核对共享池、接口并发和Worker槽，避免抢实时或饿死目录。
4. **写入与增长治理**：批量状态更新、流水归档、暂存回收、目录当前投影。先证明引用和幂等保留合同，再实施删除/归档。

主工作区另一个未提交方案正在改统一物理配额和批量管理。本轮仅标注审查事项：实际app等级证据、所有SDK/引导入口覆盖、上传容量与高inflight冲突、MCP独立合同、新旧Redis桶协调、QPM/QPD窗口历史、逻辑预检与物理发送规则一致性。**这些是未完成方案的检查项，不是已经上线的新缺陷。**

每一阶段至少记录3×15分钟及24小时结果，比较有效变化行/请求、同类μ−λ、最老任务年龄、实时P95、provider拒绝率和DB/槽秒成本。用户要的是稳定结果，不能用提高完成计数、清空队列、隐藏失败或只通过单元测试替代验收。

## 相关历史记录

- [10/19 根因排查](2026-10-19-sync-backlog-root-cause.md)、[架构复盘](2026-10-19-sync-architecture.md)
- [10/20 请求缩减](2026-10-20-sync-request-reduction.md)
- [10/27 积压记录](2026-10-27-sync-backlog.md)
- [10/30 端到端恢复](2026-10-30-sync-end-to-end-recovery.md)、[根因复盘](2026-10-30-sync-root-cause-review.md)
- [目录缺失公平性](2026-10-30-directory-missing-fairness.md)、[素材类型根因](2026-10-30-sync-material-type-root-cause.md)
- [请求效率与目录策略](2026-10-30-request-efficiency-and-directory-policy.md)、[历史事实容量修正](2026-10-30-background-fact-capacity.md)
