# 同步积压恢复（2026-11-08）

状态：第一阶段Control内存修复已生产验收；第二阶段BC锁修复本地验证完成，待精确版本发布及持续净消化验收。

## 已确认生产证据

生产67537f33/schema0101。10:11活跃父19559，10:17为20372；后者目录9422、报告10950，其中14892创建于11月5日12:39快照之后。最近一小时目录COMPLETE5482、报告COMPLETE6900；素材补链仅227完成/仍8044活跃。目录publish退避104，报告1299等待目录。

Control当前容器2Gi、三个prefork，累计78次重启且最近OOMKilled。随后再次只读采样确认最近OOM，当前memory.current约1.55Gi/peak1.80Gi，三个子进程RSS约292/776/520Mi，两个HWM约1Gi；父约238Mi。未归因全部78次均OOM。近15分钟目录28次publish deferred，确认55P03锁超时和57014查询超时；日志中任意包含429的行不作为HTTP429限流统计。

## 第一阶段具体变更

保留Control三个执行进程和每进程数据库2+1预算、CPU request250m/limit1；request内存1→2Gi、limit2→4Gi；添加Celery --max-memory-per-child 384000（KiB），--max-tasks-per-child 100，均在当前任务完成后回收子进程，不强杀正在执行任务。Recreate及原120秒退出宽限避免新旧实例双开。现有集群节点实测内存28–67%，无需增加节点或云采购。只改Control模板，不更换应用镜像、Secret/ConfigMap、平台额度、typed窗口或业务开关；原业务任务/消息/未满足水位全部保留。

这首先防止保留堆与三个瞬时峰值使整个调度进程反复OOM；不声称已定位全部内存分配来源或解决目录数据库竞争。后续核对实际子进程回收、OOM计数、目录发布慢语句、完成与新建窗口，以及真实未满足范围。

## 验证与上线

部署合同测试24项通过8.155秒，配置差异检查通过。源清单须持久保存；仅针对本项目Control、共享发布锁、resourceVersion/template CAS后切换，保持正常退出。无新备份或恢复演练。实际生产结果待续。

## 内存配置实际生效

配置源提交638b3d26；共享发布锁下仅Control模板CAS更新，保留应用67537f33。新Control pod 6865bff8f4-h4982已Ready，真实Celery stats为concurrency3/processes3/max-tasks-per-child100；命令384000KiB及资源2/4Gi与源模板一致。11角色Ready，其他Deployment spec与两项私有配置摘要保持，发布锁已释放。部署合同最终24项通过7.586秒；未推送源码。

## 第二阶段：BC外键锁竞争

12次生产PG活动采样发现经营business_run长事务idle in transaction约98秒，同BC目录ad_directory_run INSERT等待transactionid约38–58秒，并有tenant_bc FOR UPDATE锁队列。经营规划、核算及配置只需串行化经营写入，不修改tenant_bc主键；原FOR UPDATE也阻止目录/报表插入所需的外键KEY SHARE。

五处BC锁（projection._scope_lock/advance_run、tasks.work_business外层及两处配置BC写入口）改FOR NO KEY UPDATE，保持与同类/旧FOR UPDATE互斥，允许FK KEY SHARE；BusinessRun/Scope行锁、原skip_locked、顺序和发布revision/租户围栏保持，无DDL/消息/水位修改。旧新版本互斥兼容，但旧持锁事务须正常退出后才验收阻塞消失。

新增真实PG并发行为在旧码先失败，55P03的CONTEXT明确为tenant_bc外键SELECT FOR KEY SHARE；修改后57项business回归全部通过21.61秒，三源mypy通过，Ruff/格式检查通过（测试import顺序已修）。独立审查未发现Critical/Important。没有把Celery任务succeeded当业务COMPLETE，也不把字符串429匹配当HTTP限流证据。
