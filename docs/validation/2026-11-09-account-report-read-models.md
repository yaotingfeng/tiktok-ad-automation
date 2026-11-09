# 账户报表成员版本与日汇总（本地完成，未上线）

## 最终行为

首次账户报表快照只读取账户日金额、ad_day状态镜像和账户成员版本UUID，不遍历全部系列目录/名称或复制全部系列引用。当前页补全只展开页内成员；选择和导出展开快照固定的旧版本及旧名称修订，后来新增、删除或改名的系列不改变已冻结目标。

新增四张派生表：account_membership_version/current、account_report_day/state；query_snapshot_row新增版本外键及只覆盖账户行的部分索引。不可变payload由数据库触发器保护；版本退休至少一小时且没有当前指针/查询快照引用才回收，租户删除级联清理派生版本。导出和已冻结选择仍复制完整证据，可以独立于短期快照存活。

目录身份/名称变化在原发布事务内维护版本，预算/开关等不改变系列身份的UPDATE跳过成员重建；无变化payload不另建版本。账户日汇总与campaign_report_day发布同事务维护，保留NUMERIC精度、币种/时区、NULL与部分合计。持久账户日锁行使RC并发写者等待后复算、RR旧写者序列化失败，不能用旧读覆盖后来提交的金额。状态镜像只读取ad_day。

0081允许保留已删除账户的canonical历史。0103集合回填只JOIN存活账户，原历史不删；账户重新发现时，通过new_rows集合恢复历史日金额和状态，管理成员只从当前canonical目录生成，不复活已删除系列。大小transition批次用固定SQL重新规划。完整空桶只保留COMPLETE状态，金额查询沿既有规则补0。

## 验证与限制

最终独立PG18/Redis测试：19个相关测试模块 **168 passed / 33.12秒**。包含成员/名称变化、无意义更新去重、完整空桶、NULL/多币种/跨日期与源查询等价、旧快照新增/删除/改名隔离、权限及租户范围、不可变payload、事务回滚、GC与导出、实际RC锁等待及复算、RR旧事务40001、租户清理、已删账户孤立历史与重新发现。0102↔0103真实迁移往返两轮，保留原refs及逐系列名称修订，并验证存活金额的source_revision与状态revision一致。

合成5000账户/100000系列（另1个既有空账户），真实canonical写入和触发器维护后：

| 阶段 | 本地耗时 |
| --- | ---: |
| 合成发布维护（目录及系列日报各10万行） | 5.773秒 |
| 首次账户快照（5001行） | 0.217秒 |
| 当前页补全（50行） | 0.086秒 |

实际EXPLAIN检查：快照构建不扫描ad_object、ad_entity_read、campaign_name_current、campaign_report_day或成员版本payload；账户日事实读取不超过5000行，query_snapshot_row不存复制refs，快照头只有每账户UUID引用。另有单户20000系列测试约束请求与成员数脱钩。

上述是本地合成测试，PG实例用于功能/锁测试，缓存没有清空，不代表生产端到端或磁盘耐久性、冷缓存SLO。没有将其与生产25–32秒直接计算提速倍数。生产必须上线后再测相同范围的完整接口、热/冷首屏、同步写入延迟/锁等待/积压和Worker RSS。

8个修改实现模块严格mypy、Ruff/格式、语法及差异检查通过。无前端/API响应合同改动，不生成客户端或运行无关前端构建。独立代码审查发现孤立状态回填外键问题，先用回归复现，再修复为集合过滤/恢复并静态复审闭环。早期规模夹具参数类型、租户清理外键及账户重新发现漏回填均已针对性修复，不以初次运行冒充通过。

广泛报表目录试运行曾中止排查，不能宣称全目录通过：test_background_execution_share::test_arriving_facts_reclaim_next_released_slot_without_cancelling_work与test_backlog_control::test_missing_video_waits_for_directory_without_recreating_work已在未修改HEAD的独立归档/专用库同样复现。其余早期清理污染和本轮新增失败已在最终168项组复验；没有夹带修改调度实现。

命令：使用专用测试环境，在backend执行pytest针对test_account_read_models/migration/scale、queries、dimension_query、selection、exports、query_reuse、query_page_cost、large_account_query、sync_api、api、read_model_projection/rebuild、projection_task_budget、publish、publish_watermark、balance_claims及balance_compaction。连接参数不提交；私有完整日志/JUnit与合成计划在`.runtime/report-optimization-20261109/`。

## 具体发布边界

本轮只本地实现/验证/提交，不推送、不修改生产DDL或业务数据。只读核对时生产API镜像为be53d533e954ee03d7ba6fc20263994c77266c8d，数据库仍0101_business_dashboard；不冒称已核验全部11角色或本优化已生效。当前本地main包含另一个未发布0102_business_rate_periods，以及其他独立改动；0103以0102为父修订，不能把整批main当无迁移小修直接发生产。

发布前必须选定精确程序与数据库范围，审查0102是否一并发布；遵循现行按需备份政策，备份/恢复方案另行选定，不自动执行或冒称已有恢复验证。预检存活账户、历史账户日桶、孤立源事实/状态、活跃查询快照数量和空间；保留源事实及用户配置。

迁移窗口暂停新查询/写入受理，正常排空会写canonical数据或查询快照的旧进程，再执行0103和集合回填，API及Worker/Beat统一切换；旧API不能继续插入旧账户快照格式。保持原租户/BC授权、四开关、调用额度、进程资源和任务队列，不新增平台请求或广告操作。首次上线回读金额/覆盖、旧快照/选择/导出和写入吞吐，不能仅健康200验收。

程序回退须配套数据库：0103降级先将版本展开回旧账户快照refs与名称版本，再删除新表/触发器/字段；已有冻结选择/导出保持原样，canonical事实不删除。生产0102的独立回退约束仍按经营比例发布手册，不能由0103降级绕过。
