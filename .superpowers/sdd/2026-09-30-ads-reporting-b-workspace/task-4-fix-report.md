# B4 复审修复

- 导出创建在同一数据库事务写入 `PendingDispatch`，任务键按导出 ID 幂等；事务回滚不会留下孤儿队列消息。新增 `reporting.cleanup_exports` 清理任务并加入 ads-reporting Beat。
- 导出 worker 遵循 outbox 的 `tenant_id`/`actor_id`/`payload` handler 合同，重新校验任务作用域后才生成。
- 下载只接受当前租户/BC/导出 ID 计算出的对象键；过期清理删除专属对象并清空 `object_key`，失败生成不会写出可见对象。
- 已访问而被标记 `EXPIRED` 但仍带对象键的记录仍进入清理扫描；授权重建失败的队列任务转为 `FAILED`，不会永久保持 `QUEUED`。
- 迟到任务发现已完成但当前撤权的导出时，只删除该导出按租户/BC/ID计算出的专属对象并清空键；越界对象键保留并继续拒绝下载。
- 相同租户、BC、操作者和幂等键通过唯一约束及 savepoint 重读，重复请求稳定返回同一导出。
- 查询和个人视图将不支持的排序字段映射为 422；不调用 TikTok/MCP/provider。

验证：B4 模块 ruff、ty、compileall 与 `git diff --check` 通过。当前 shell 仍未注入专用 PostgreSQL `test.env`，pytest 会在仓库数据库守卫收集阶段停止。
