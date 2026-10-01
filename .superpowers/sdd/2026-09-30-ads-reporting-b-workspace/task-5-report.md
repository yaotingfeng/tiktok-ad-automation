# B5 六维广告工作台报告

完成六维广告报表工作台：账户、系列、广告组、广告、素材、剧页签共用搜索筛选与分页；汇总 D0 收入、消耗、D0 ROAS 和目标 ROAS；SVG 趋势及无障碍数据表；详情侧栏、覆盖状态、外部广告提示和固定官方后台跳转门禁。TanStack Query 按租户/BC/筛选/快照建立查询键，BC 切换清空选择并由请求信号淘汰迟到响应。分页沿用 B3 snapshot_id，筛选/维度切换重置 cursor、snapshot 和跨页选择；相同筛选应用/重置也会创建新查询 revision，避免旧 row_key 冻结；BC 切换首个请求不携带旧 BC 快照。摘要消费 B3 buckets，混币种/时区/归因桶显示多口径或覆盖不完整；B3 未发布 target_roas 时明确显示目录未同步，不把 roas_bid 当报表事实。刷新、导出、保存筛选和批量冻结均调用生成客户端 API，并展示进行中/失败状态；viewer 前端和后端均禁止刷新同步、导出、保存视图和批量冻结，查询和既有数据读取仍为 read。详情请求携带当前 snapshot_id，后端校验该快照租户/BC/有效期后读取 operation 状态。新增 reporting_write 权限动作，允许 operator 编辑报表偏好/冻结选择/触发同步，viewer 只能 read。

验证：

- `bash scripts/generate-client.sh`
- `npx tsc -p frontend/tsconfig.build.json --noEmit`
- `npx vite build`（等价构建检查；环境未安装 Bun）
- `PLAYWRIGHT_BASE_URL=http://127.0.0.1:5173 npx playwright test tests/ads-workspace.spec.ts tests/ads-reporting.spec.ts --project=workspace`（8 passed，含快照分页、详情 operation、混币种桶、相同筛选重置和 BC 快照隔离）
- `npx @biomejs/biome check src/features/ads tests/ads-workspace.spec.ts tests/ads-reporting.spec.ts tests/utils/adsBoundary.ts src/routes/_layout.tsx src/features/tenants/TenantScope.tsx 'src/routes/_layout/tenants.$tenantId.ads.tsx'`
- `cd backend && uv run --frozen ruff check app/modules/reporting`（通过）
- `cd backend && uv run --frozen pytest tests/modules/reporting/test_api.py -q`（未运行：测试启动前拒绝无专用 PostgreSQL DATABASE_URL，未伪造通过）

未调用真实 TikTok/MCP、未写广告、未部署或推送。修复提交：`a2a9f82`、`e9ab5dc`、`1aed8d5`、`c926f61`、`172b470`、`cca9801`、`15bb0a2`、`15f9a9b`、`da0067c`，提交主题分别为 `ads: fix reporting workspace snapshot and actions`、`ads: keep read-only reporting actions available`、`ads: harden snapshot detail and bucket handling`、`ads: keep target coverage fixture contract honest`、`ads: isolate BC snapshot transitions`、`ads: save only supported view columns`、`ads: label empty coverage explicitly`、`ads: bind detail snapshots and viewer actions` 与 `reporting: enforce write permissions at API boundary`。
