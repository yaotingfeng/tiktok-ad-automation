# 同步积压生产恢复（2026-11-09，继续中）

用户明确授权自行推送、发布生产及继续修复，不重复请求推送或发布确认。第一阶段已上线；历史缺口与SQL发布超时尚未全部解决，不声明积压清零。

## 精确版本与范围

候选a6cda471cff212ac3ddff96030ce1efe851eabca分别快进推送公司Gitea/main及GitHub origin/main；没有推送或发布后续其他会话的SDK错误诊断/音乐改动。生产镜像索引sha256:3447e5b4851c4b93c55b19779f574a62414c5a24417cbcb9b71bf8315637e214，实际AMD64容器四个相关模块hash与候选相同，Python3.14.10，SQLAlchemy实际生成FOR NO KEY UPDATE。业务57项真实PG回归、三源mypy/Ruff/格式、部署合同24项及两轮独立审查证据沿用11月8日相同源码，不重复无关大范围测试。

共享发布锁及原CI暂停，正常退出宽限保持。API/前台各Worker先更新，背景取消两个consumer后连续两次active/reserved/scheduled/queues全0，零副本后切换再启动；Beat Recreate最后。最终11角色/9Worker Ready、唯一Beat、0101/schema、精确摘要、四开关/私有配置/DB池/HTTPS均通过，后台实际8/typed8/prefetch12保持，Control3/request2Gi/limit4Gi/384000KiB和100任务回收保持。发布锁释放、Jenkins恢复原buildable=true且当时无在跑/排队；未备份、未恢复演练。

## 重复补链清理

旧生产675与候选a6的canonical pending_report_compaction源码完全相同（SHA256 9a34127044e3f1092843efcfe57d9d3932126126f7f69e267b6cfa98d03dd97f）。完整发现3309对未截断；逐对回滚预演2100合格、1208跳过、1项57014待复查。趁镜像上传，在稳定旧API675、同schema0101及精确canonical hash保护下执行，所有维护结束并回读前不开始rollout。每批128/20秒、短事务、共享维护锁不抢占、单次journal/提交receipt，未知结果不重放。

实际2035取消、65因实时条件变化跳过、0提交延期；全部journal闭合无unknown。独立RR回读2035旧父均CANCELLED/pending_superseded且token/lease清空；2041旧引用无PENDING、无owner变化、无承接缺口，其中2040仍由当前新PENDING引用承接，1已真实满足；204个最终承接父全部存在（167WAITING/36QUEUED/1COMPLETE）。旧head有效租约0。取消没有推进业务满足水位，不算真实同步完成。

## 首个稳定窗口

01:32:40–01:38:48，北京时间约6分钟，无本轮手工取消：活跃父19415→18330（目录12360→11638、报告7055→6692），真实COMPLETE1464（目录780/报告684）；另20STALE和1系统CANCELLED，推算新增或重新入队400。按终态时间及期初期末活跃数估算有秒级事务边界误差，不能外推未来清空时间。

只读冻结01:20:31的76585个未满足需求版本，压缩UUID/版本基线完整传输；01:32与01:38分别RR回读全部ID，missing均0。首稳定窗口同一旧版本真实新增满足696，来自目录689/素材关联7；没有把新需求、水位重置或父取消计作完成。余额8043、素材关联49606和创意目录2815等原基线缺口仍大，不能宣布历史补齐。早期大型JSON导出遭kubectl EOF已废弃，仅用完整压缩基线。

## 后续待定位

新目录日志仍有SQL57014退避与一次失败；20次PG活动采样已不见此前tenant_bc外键锁队列，主要IO等待及ad_object集合UPSERT约16秒。发布Worker四进程共限1CPU，151秒采样使用0.781核、45.1%期间被CFS限速，内存1110MiB/峰值1427MiB/无OOM。背景CPU1.251/限3、reporting0.212/限4、Control0.354/限1，均无OOM；不据此盲目扩槽。PG实际max_connections600，本次采样约55连接，容量判断不再沿用旧200上限。下一步检查ad_object触发器/执行计划及发布CPU瓶颈，继续真实窗口验收。

## 本机构建问题

首次构建因Docker数据盘满退出，生产未变。仅删除60个未被任何容器引用的本项目旧本地镜像缓存，保留最新12及所有容器镜像，不删卷、数据或云仓库镜像；fstrim后主机可用空间3.8→约11GiB。重建完成，构建器上传缓慢后改为直接推送同一已校验镜像，最终Docker push退出0且摘要相同；没有在上传未完成时切换生产。完整证据在忽略目录.runtime/sync-production-20261031/hotfix1108及pending-cancel1109。
