# 2026-11-09 素材读取 Y 与精确承接清理 Z

应用 `18b19574826f6f92e5fdd21e5d6e19e942dbe800` 已完成公司/GitHub推送、构建和生产全11角色验收，实际平台摘要 `sha256:7164b8c8e3706abf8b93da43d37c3fca5edc9ce4f8a5cda56efee72d149902ce`。后台正常排空到连续两次零在途、零旧Pod后更新，Beat最后；schema0101、配置/四开关/SDK额度/DB池不变。后台12/typed12/prefetch12、CPU3/内存10Gi、报表12和publisher8保持，锁释放且CI恢复。没有DDL、新备份或恢复演练。

素材读取在单量子内只完整stream和构造一次冻结DTO，并在领取提交后只准备一次不可变桥索引；下一量子重新验证。publisher/末页仍完整验证但不保留素材DTO，逐HTTP owner/gen/head/route/TBR围栏保留。113项后续防重复回归属于另一待发布修复，不计入本应用；本应用X2已有79项针对性回归、精确干净候选35项通过，Ruff/格式及tasks.py mypy通过，adapter历史类型错误无新增。

Y约197.6秒采样后台平均1.891/3核，17.5%调度周期受到节流，内存3759.9MiB/10Gi；与W不同时长/任务混合，不能计算整体加速比例。当前平均CPU仍有余量，CPU6与扩槽试验只准备而未执行。

## 精确同范围旧父清理

完整只读发现846对，同一原canonical守卫预演668合格。仅固定668名单进入生产执行；329个宽日期包含候选未纳入。已取得原全局维护锁并暂停CI，每批核原配置、全部部署spec/UID及11唯一角色的实际digest/PodUID，Redis cleanup锁互斥。每对调用原supersede_pending_report、独立短事务并锁时重验，固定后继不猜配对，50秒维护进程硬终止及连接/SQL/事务/锁预算保护；未知提交停止、不自动重放。

实际668对中590旧父CANCELLED/pending_superseded，78因条件变化跳过；不是删除事实或声称旧数据已被实际发布覆盖。独立只读回验：590父均无租约、无旧PENDING引用、后继承接缺口0、未知0；其后部分后继已自然完成允许通过。取消只收口旧重复执行，原canonical在全部锁内断言desired/satisfied/requested_after不变，不抬观察水位。取消不计成功吞吐。维护finish已核本轮SHA、manifest hash、668逐对回执/unknown0及完整回验后才解锁恢复CI，无active/queued构建。

私有证据为 `.runtime/sync-production-20261031/hotfix1109y/` 和 `pending-compaction1109z/`；固定manifest与身份仅保存在私有目录。

11:05积压目录3947、报表5171、总9118，仍未清空。真实报告底座主要当前未满足需求，不能按旧created_at或历史业务日期无条件取消。防重复续步复用及目标素材精确键查询已完成本地回归，等待独立精确候选发布；来源素材补派长历史查询仍待解决，不宣称全部问题处理完毕。
