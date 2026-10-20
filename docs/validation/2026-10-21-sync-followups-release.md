# 同步精简后续四阶段生产发布（2026-10-21）

## 版本与授权

用户在阶段4执行期间追加要求“处理完以后推送并发布到生产环境”，替代原不推送/不发布限制。阶段4产品提交为 `340108c711ec375c5acccea88dc207ff84e4c016`（`reporting: 零金额帖子不再阻断素材止损或重复补链`），直接接在依赖审计 `ff0bb7b5` 后；本次同批发布先前未上线的阶段1 `53a5888f`、阶段2 `23abca2d`、阶段3 `d2d78398`。没有改写这些提交，也未创建分支、工作树或子代理。

精确版本已推送 origin/main 与 company/main。公司推送自动触发 [Jenkins #101](http://123.207.4.46:8080/job/tiktok-ad-automation/101/console)，最终 SUCCESS；私有批次 `20261020T173542-19769c`。生产目标仍为新加坡 ACK 的 `test` 命名空间、https://tkada.shortclipflix.com 。发布器于北京时间 **2026-10-21 02:06:20** 完成。

11角色全部运行340108c7，镜像 digest `sha256:ad247c9acd5bf76944751c40b063ae75234e7cf3c1f77cf07f8a0167c5c94d2a`。没有新增schema，正式head仍为 `0042_drop_ad_material_main`。四业务开关仍全部true；9个Worker数据库池2+1，API/Beat5+10，原并发、运行配置和配额保持。发布器逐角色验配置/镜像、唯一Beat、9个Worker就绪及HTTPS200通过。

## 阶段4改动文件与回归

| 范围 | 文件（路径前缀后列文件名） | 结果 |
| --- | --- | --- |
| backend/app/modules/reporting | 新增material_value.py；material_links.py、jobs.py、directory_refresh.py、material_identity.py、spark_identity.py | 按账户+帖子在各窗口跨日合计金额，五处判定共用；混合任务的概览/身份/目录候选也排除零金额，等待任务自然结束 |
| backend/app/modules/automation_rules | material_inputs.py、inputs.py、data_issues.py | 零金额缺关联不阻断止损，付费指标排除零金额曝光，运行摘要持久保存按账户+帖子去重的忽略计数 |
| backend/tests/modules/reporting | 新增test_material_value.py；test_material_resume.py、test_material_links.py、test_backlog_control.py、test_backlog_dependencies.py | 保留8bd35045原用例和索引执行计划验证，扩展零金额/仅收入、窗口合计边界、隔离、混合任务和等待收敛 |
| backend/tests/modules/automation_rules | test_material_inputs.py | 两种原阻断场景命中恢复为1，正消耗/仅收入缺口仍保护，跨日窗口、空VID、忽略计数持久化和付费曝光 |
| frontend | src/features/automation-rules/dataIssues.ts；tests/automation-rule-form.spec.ts | 现有运行详情明确显示忽略数量及“不影响规则判断” |
| docs | implementation-progress.md；superpowers/specs/2026-10-d；superpowers/plans/2026-10-21-sync-followups.md；validation/2026-10-21-sync-followups.md | 新授权、设计、计划、测试及范围调整记录 |

产品提交共21文件。未修改row_set.py、material_query.py、health.py；零金额帖子仍保留真实未映射独立行，不写假VID或假已关联状态。原8bd35045是为了保留零花费非空事实并完整映射，规则及页面依赖确实存在；用户追加批准修复规则保护后才同时收窄五处判定，审计事实保留在[原交接](2026-10-21-sync-followups.md)。

本地门禁：真实独立PG17.5/Redis8.0.1；三模块 `uv run pytest tests/modules/reporting tests/modules/automation_rules tests/modules/ads -q` **1034 passed / 58.68s，无跳过，净增44项**。`ruff check app tests`、`ty check app`通过，15个改动/新增Python的`ruff format --check`通过。mypy为499条/74文件，对ff0bb7b5的502条按规范化诊断多重集无新增；mypy本身仍有存量错误，不记为全量通过。前端两文件Biome、应用tsc通过，Playwright文案1项通过。过程失败和修复如实记录在实施进度。

Jenkins独立发布检查：affected 30个文件、后端70个测试文件，**1017项全通过，0失败/0并行不稳定**；Ruff/ty通过，前端构建及工作区 **626项通过**。后端检查1分09秒，前端5分01秒。CI按导入依赖选测与本地用户指定的三模块全量范围不同，两份结果分别保存。

## 完整发布过程

本次包括jobs.py和持久化调度行为的变化，既有发布器按其规则进入full模式，正常排空后完整冻结备份及隔离恢复；没有人工提前结束备份、清队列、改等待任务状态或触发TikTok接口。恢复原有生产消费者后，其正常调度继续执行。

- 01:42:04停止生产者，01:42:49冻结完成；01:50:21备份完成，耗时452秒。
- 02:04:33恢复核验通过，备份完成后核验耗时852秒；170表一致、6份凭据可解密、57个Redis键恢复验证通过。
- `/backup/latest`位于`tiktok-ad-automation-backup` PVC，备份源版本48197483，consistency=frozen；manifest SHA256 `10afee9be295d9ecbaf37a689f42f9fefcba70772f36529b2835f17e879b882d`。独立程序/构建、配置、Beat、数据库及Redis均纳入。
- broker检查移动0条；正式Alembic保持0042，02:05:10候选部署完成；02:05:32开始恢复，02:06:20验收完成。生产者停止至完成24分16秒，不把它描述为快速无停服发布。
- 运行证据仅在Jenkins私有批次及本机忽略目录`.runtime/sync-followups-release/`，不提交私密配置、真实业务明细或备份。

## 上线只读观察

上线前46个关联等待任务，其中42个身份缺口任务累计请求11668次；已固定保存这46个任务的原始ID及请求数用于逐项比较。三个真实等待窗口的新金额聚合只读采样为4–44毫秒，不能视为所有历史窗口的性能上限。

02:07回读：原46个任务中 **44个COMPLETE，且这44个全部未新增平台请求**；剩余2个WAITING各有1个有金额缺口，继续保留缺数保护。均由正常依赖重查/领取/执行收敛，无人工状态或映射更新。该固定集合与上线后新投放产生的新关联任务分开统计。

两个BC的next_hot_at/next_material_at已经完全相等；规则BC为UTC18:12:00，无规则BC随后一起推进到18:22:04.321727，原独立相位保留。规则同步前置3分钟生效。

02:07恢复初期出现一次MONITOR_QUERY_FAILED和停服期间报表/目录陈旧；02:08查询失败已消失，一个BC报表陈旧账户归零，另一个仍在追平。不得将此快照记为业务全绿。

02:10两个BC的广告/素材陈旧账户均归零；02:12规则BC按期规划并把两个时钟一起推进至UTC18:27，非规则BC保持18:22:04原相位。02:15快照一个BC为HEALTHY，另一个剩9个目录缺口、3个未关联帖子，仍为DEGRADED；PG本项目31连接、2个活跃、0锁等待。新投放的关联任务也由正常Worker完成，固定原集合仍剩2个有金额等待。

18:15 UTC正常刻度的三条启用规则已完成，实际运行记录分别保存 **2、8、2** 个零金额未关联帖子的忽略计数，命中数分别 **1、5、0**；这些是各规则窗口内的计数，不能跨规则相加当作全局唯一帖子数。此轮已无STALE_REPORT，仍保留MATERIAL_LINK_MISSING等原保护。恢复后最初补跑的17:45槽位因停服期间数据过期各有170个STALE_REPORT，未误用陈旧数据；18:15正常运行后next_run_at均为18:30。未人工触发规则或广告操作，不把规则命中数等同于已完成广告写入回执。

生产只读探针第一次使用了不存在的automation_rule_run.created_at列，两个统计查询报错；改为模型中的started_at后回读成功。探针使用READ ONLY事务和8秒语句期限，没有产品源码/数据变更，也未把报错当作零计数。

## 范围决定与后续风险

- 健康告警拆分按用户批准范围取消：MaterialLinkScan负证据只证明无广告归属，不能证明缺VID。没有新增platform_unlinked_material_posts，剩余真实缺口继续告警，后续依据上线观察再设计。
- 补链执行候选同步过滤和规则付费指标聚合是本次已批准业务语义的必要实现；页面事实/归并、20分钟新鲜度、规则刻度保持。发布/线上只读核验属于用户后续追加授权；未人工调用真实TikTok、执行广告操作或直接改数据库任务状态。
- 原未跟踪文件全部保留。本地专用测试PG/Redis已停止，未影响其他本地实例。
- 继续关注规则网格前置时延、UTC网格请求集中、跨日收尾与今日任务共享Worker/平台配额、长窗口金额聚合负载，以及实际有金额但平台身份未解的剩余缺口。单次上线观察不能证明长期p95、切日容量或所有规则业务判断均已验收。
