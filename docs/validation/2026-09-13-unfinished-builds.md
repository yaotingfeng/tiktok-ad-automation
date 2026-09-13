# 未完成搭建入口验收

用户确认保留“保存草稿”，补齐从广告搭建页面找回并继续未提交批次的入口。

## 行为

- 广告搭建新增“新建搭建 / 未完成搭建”切换。切换走路由，复用输入页离页守卫；刷新保留当前入口。
- 列表按当前租户、BC 和最近修改时间查询，展示前三条非空去重剧名、非空输入行数、已解析账户数、策略版本和当前状态。输入行数包含重复原文，不解释为有效账户数量。
- DRAFT 恢复完整原输入；PREPARING、READY、BLOCKED 恢复原准备页；当前修订已有 BUILDING/FROZEN 预览时恢复原预览。过期修订不作为继续入口。恢复导航不附带 prepare，也不启动准备、取链或广告创建。
- 有提交记录的批次从“搭建任务”继续处理，不列入未完成搭建。只读成员可查看；权限失效隐藏缓存行；切租户/BC 重建列表和分页状态。
- 新增 `GET /api/tenants/{tenant_id}/build-drafts?bc_id=...`，默认 50、最多 100 条，游标绑定租户/BC/目录类型；先分页再聚合该页输入，不展开剧目和账户组合。只读取现有表，无迁移、功能开关和业务数据改写。

## 验证

本轮在共享工作区有另一项准备页修改时，将本次拟提交内容单独导出到私有 `.runtime/draft-catalog-verify/`，使用固定源文件执行回归，确认不依赖其他未提交改动。

- 独立本地 PostgreSQL：`pytest tests/modules/builds/test_draft_catalog.py tests/modules/builds/test_drafts.py tests/modules/builds/test_draft_connections.py -q`，21 passed。其中新增目录 7 项覆盖租户/BC隔离、只读成员、HTTP参数、分页同时间排序、最近修改、游标拒绝、空白输入、2001账户输入汇总、当前预览恢复和提交后移出。
- `playwright test --project=workspace tests/build-preparation.spec.ts tests/build-preview.spec.ts tests/build-channels.spec.ts --workers=4 --reporter=line`，81 passed。其中新增9项覆盖保存后找回、离页守卫、准备中恢复、预览恢复、只读和BC切换、分页刷新、403隐藏及390/1440布局。
- TypeScript/Vite 构建、三个后端实现文件 strict mypy、Ruff、Python编译与新增页面 Biome 通过；390/1440页面截图已人工查看，窄屏表格在卡片内横向滚动。
- 前端接口使用传输替身，后端使用独立测试库；没有真实 TikTok/版权方写入。本记录为本地验证，未推送或部署。
- 准备页改动 `d27d0e3` 提交后，本次内容重新叠加该版本独立导出到 `.runtime/draft-catalog-integrated/`：增加 `test_input_progress.py` 联合验证，后端23项、页面83项全部通过，TypeScript/Vite构建通过。此为最终提交基准；与前面的测试覆盖重合，不累加。

聚焦提交主题：`builds: add unfinished draft list and resume entry`。
