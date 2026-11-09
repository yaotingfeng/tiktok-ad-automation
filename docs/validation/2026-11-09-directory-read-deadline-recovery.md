# 2026-11-09 读取期限结束后归还资格

状态：代码与独立审查完成，待精确生产发布。

生产5分钟日志复现 tiktok_call_deadline_exceeded / read_deadline_exceeded。原读取任务已结束，却仅释放Redis queued；PG父claim90秒、head grant240秒仍保留，后续领取还须等待head到期及Beat/公平轮转。

非纯本地发布、已取得failure_fence时，仅两个已证期限码或retryable=True的tiktok_local_resources_unavailable进入规范WAIT恢复。锁父后复验tenant/actor/generation/owner/token、正式版本与终态，再完整排序锁定heads；若head token/执行/代数已变，父和head都不修改。先按原head token和旧代调用directory_outcome，之后父换代并清租约，保留next_page及已提交分页，设置1秒due后经原调度重新竞争并补位。不会直接重发HTTP，不退款实际发送成本；平台响应、非法期限/契约错误、不可重试资源错误及纯发布路径保持原处理。

恢复独立事务3秒、父SKIP LOCKED，避免读取已消耗45秒后恢复再等锁20秒并撞soft55/hard60。head完整锁不可SKIP遗漏新grant；head争用导致事务超时则整次回滚，保持旧状态并抛出，不冒充归还成功。

验证：真实PG18/Redis旧7红/6绿，新原13重点及相关5文件72项通过26.75秒；恢复争锁新增两项旧2红10.28秒，最终15重点通过7.13秒。覆盖两次真实计费事实observed_units=2/charged=5保留、第1页已提交、真实dispatcher换代并从第2页续读、新head/旧父同fence、owner/gen/token竞争、parent跳锁与head3秒超时回滚。严格tasks类型、Ruff、格式、差异通过，独立审查无Critical/Important。最终争锁修订后未重复无变化的大套测试。

无DDL/新业务开关/接口额度变更，沿用用户授权同结构快修，不新建备份/恢复演练。上线保留四开关、schema0101、发布8/后台12与当前资源；需按生产实际WAIT、租约回收、续页和完成量验收，不能以本地通过声称积压清零。
