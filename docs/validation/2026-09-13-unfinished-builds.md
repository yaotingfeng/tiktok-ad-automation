# 草稿箱与搭建恢复入口验收

用户确认保留“保存草稿”，补齐找回入口；随后明确广告搭建应直接进入新建表单，通过右上角“草稿箱”按钮查看列表。当前行为以此要求为准。

## 行为

- 广告搭建直接显示新建表单，右上角“草稿箱”按钮打开侧边列表；移除标签切换及 `view=drafts` 路由参数。只有打开草稿箱才请求列表，关闭或按 Esc 返回原表单，保留未保存输入。
- 选择“继续搭建”时关闭侧边列表并导航到原草稿/预览，复用原输入页离页守卫；取消离页保留输入，确认后才继续。切租户/BC重置草稿箱开关和列表状态。
- 列表按当前租户、BC 和最近修改时间查询，展示前三条非空去重剧名、非空输入行数、已解析账户数、策略版本和当前状态。输入行数包含重复原文，不解释为有效账户数量。
- DRAFT 恢复完整原输入；PREPARING、READY、BLOCKED 恢复原准备页；当前修订已有 BUILDING/FROZEN 预览时恢复原预览。过期修订不作为继续入口。恢复导航不附带 prepare，也不启动准备、取链或广告创建。
- 有提交记录的批次从“搭建任务”继续处理，不列入未完成搭建。只读成员可查看；权限失效隐藏缓存行；切租户/BC 重建列表和分页状态。
- 新增 `GET /api/tenants/{tenant_id}/build-drafts?bc_id=...`，默认 50、最多 100 条，游标绑定租户/BC/目录类型；先分页再聚合该页输入，不展开剧目和账户组合。只读取现有表，无迁移、功能开关和业务数据改写。

## 验证

初版列表在共享工作区有另一项准备页修改时，将本次拟提交内容单独导出到私有 `.runtime/draft-catalog-verify/`，使用固定源文件执行回归，确认不依赖其他未提交改动。

- 独立本地 PostgreSQL：`pytest tests/modules/builds/test_draft_catalog.py tests/modules/builds/test_drafts.py tests/modules/builds/test_draft_connections.py -q`，21 passed。其中新增目录 7 项覆盖租户/BC隔离、只读成员、HTTP参数、分页同时间排序、最近修改、游标拒绝、空白输入、2001账户输入汇总、当前预览恢复和提交后移出。
- `playwright test --project=workspace tests/build-preparation.spec.ts tests/build-preview.spec.ts tests/build-channels.spec.ts --workers=4 --reporter=line`，81 passed。其中新增9项覆盖保存后找回、离页守卫、准备中恢复、预览恢复、只读和BC切换、分页刷新、403隐藏及390/1440布局。
- TypeScript/Vite 构建、三个后端实现文件 strict mypy、Ruff、Python编译与新增页面 Biome 通过；390/1440页面截图已人工查看，窄屏表格在卡片内横向滚动。
- 前端接口使用传输替身，后端使用独立测试库；没有真实 TikTok/版权方写入。本记录为本地验证，未推送或部署。
- 准备页改动 `d27d0e3` 提交后，本次内容重新叠加该版本独立导出到 `.runtime/draft-catalog-integrated/`：增加 `test_input_progress.py` 联合验证，后端23项、页面83项全部通过，TypeScript/Vite构建通过。此为初版列表提交基准；与前面的测试覆盖重合，不累加。

聚焦提交主题：`builds: add unfinished draft list and resume entry`。

## 草稿箱按钮交互调整

- 仅修改前端入口与列表容器；后台目录、草稿保存、解析、预览和提交逻辑沿用原实现。
- `build-preparation.spec.ts`、`build-preview.spec.ts`、`build-channels.spec.ts` 共84项通过；覆盖直接显示表单、按需加载列表、关闭/ESC/焦点恢复、未保存输入保留和加载草稿时的离页提醒、跨BC、分页、权限与预览恢复。
- TypeScript/Vite构建、改动文件Biome和差异检查通过；390/1440默认入口及草稿箱截图已查看。前端仅使用传输替身，无业务写入，未推送或部署。
- 聚焦提交主题：`builds: open draft box from the new build form`。
