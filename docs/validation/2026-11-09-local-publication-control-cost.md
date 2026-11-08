# 本地发布与HTTP成本隔离、经营来源按值读取（2026-11-09）

用户授权自行推送生产及继续修复。前轮十二进程候选a6a95dd1正在发布，本轮不会与其争用发布锁或并发部署。

## 实际缺陷

ab8973de稳定近十分钟，publisher有5次ads.publish_step在execution_cost_context首次producer登记时被sync_cost_ledger行锁55P03打断，此时尚未进入本地发布的原重试保护。纯本地publisher只允许发布已持久化末页，缺末页明确拒绝，绝不读取远端；零HTTP步骤仍登记成本producer导致无意义依赖。仅publish_only改为空上下文，原目录读取仍在任何HTTP前required持久化producer；未知收据/重试计数/原始冻结额度与公平结算保持，租户、actor、owner、代数、末页及发布原子围栏不变。

同窗Control有4次business.schedule软超时，堆栈落于source_snapshot季度ReportSyncState批量ORM.all及cleanup_business候选加载。经营来源只需8个状态值，改SQLAlchemy映射列查询、返回值行，保留Session自动flush、过滤/排序及同一签名/覆盖逻辑；不逐行构造ORM对象。清理候选load_only仅id/tenant/bc/quarter/status，不加载大inputs/coverage，原FOR UPDATE SKIP LOCKED、发布指针与有效导出保护及派生表分批删除保持。

## 验证及发布边界

三项真实PG18新回归旧码均红：实际持有成本行锁导致本地发布55P03；来源签名构造34个ORM状态对象；清理SELECT包含大JSON。新码126相关回归通过50.09秒（全business、目录同步/量子/素材上下文），类型注解整理后核心5项再通过3.53秒；三源严格mypy、Ruff/格式通过，独立审查无Critical/Important。锁竞争测试同时断言本地发布完成、HTTP读取次数不增加、原成本行全字段不变。来源测试核对全部快照及签名一致。

计划精确干净候选接a6a95dd1，保持十二后台/typed12/prefetch12、memory10Gi、publisher4/2CPU和所有其它角色资源、四开关、BASIC额度、DB池与0101。共享锁/CI暂停及恢复、精确双端SHA/digest、正常退出/后台两队列排空零Pod后换镜像、Beat最后及全11角色验收保持。无DDL/消息合同变化，不新建备份或恢复演练，不执行真实广告写入。发布后核对经营next_check与异常、publisher行锁错误及一致积压/真实完成；待验，不宣称清零或长期无错误。
