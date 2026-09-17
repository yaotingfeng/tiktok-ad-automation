# 跨 BC 素材准备根因与修复

目标为新加坡独立测试服；生产未纳入范围。用户要求排查并修复已提交 22 剧中 18 剧可搭建的批次，18 剧 × 10 账户共 180 组合。

## 根因证据

- API、resources/builds/control Worker、Beat 全部运行；队列持续消费。素材准备失败导致后续 Campaign 等待，排查时 builds 队列约 4,800 条。
- 失败素材步骤未保存 distribution，通用错误 `execution_prepare_failed` 来自执行器捕获非 DomainError。
- 在真实数据上仅调用本地准备函数并回滚事务，无远端请求，得到 PostgreSQL `23514`、`material route connection scope mismatch`。调用链为 `ensure_target_asset → ensure_bc_seed → ensure_target_asset → flush`。
- 数据库 `mcp_material_route_guard` 对 source_route 和 target_route 均使用 `b.bc_id=NEW.bc_id`。来源连接只有来源 BC 授权，合法跨 BC 转存被按目标 BC 误拒绝。
- 历史迁移已把 source_route 的 CHECK 改为 source_bc_id，但漏改连接归属触发器。原跨 BC 测试将同一连接绑定两个 BC，掩盖该差异。

## 修复与验证

前向迁移 `material_route_scope` 仅调整函数中的绑定 BC 比较，使用当前冻结路由的 bc_id。字段 CHECK 继续将 source_route 绑定 source_bc_id、其他路由绑定目标 bc_id，租户、通道、不可变检查保持。迁移不更新历史业务行，不增加开关或依赖；存在跨 BC 冻结记录时拒绝降级。

真实本地 PostgreSQL / Redis：

- 新回归先失败：合法独立来源授权被拒绝；仅绑定目标的连接错误通过来源检查。
- 素材来源/目标独立绑定、错误连接/租户、冻结不可变、跨 BC 转存与并发、失败恢复、远端原件读取、历史路由迁移共 64 项通过。
- 增补历史数据库升级、无跨 BC 冻结记录时往返、存在跨 BC 冻结记录时拒绝回退的专项验证。
- 首次扩展测试 62 passed / 1 failed，因本地应用 Redis 指向未启动端口而返回 503；改用独立本地测试 Redis DB 13/14 后 64 项通过，没有改业务代码规避该失败。

部署及原批次恢复尚待执行，不能将上述本地结果视作服务器已修复。发布前必须备份数据库、Redis、独立项目归档及配置/证书，并在隔离实例恢复核验。原失败步骤使用系统 `RETRY` 恢复流程，沿原冻结预览、账户、连接和请求身份执行；已发送或未知结果不能直接重建。
