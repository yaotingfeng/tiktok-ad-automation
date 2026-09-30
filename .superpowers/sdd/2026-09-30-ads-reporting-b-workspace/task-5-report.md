# B5 六维广告工作台报告

完成六维广告报表工作台：账户、系列、广告组、广告、素材、剧页签共用搜索筛选与分页；汇总 D0 收入、消耗、D0 ROAS 和目标 ROAS；SVG 趋势及无障碍数据表；详情侧栏、覆盖状态、外部广告提示和固定官方后台跳转门禁。TanStack Query 按租户/BC/筛选/快照建立查询键，BC 切换清空选择并由请求信号淘汰迟到响应。新增刷新、导出、保存筛选入口，并保持 shadcn 表格卡片视觉规范。

验证：

- `bash scripts/generate-client.sh`
- `npx tsc -p frontend/tsconfig.build.json --noEmit`
- `npx vite build`（等价构建检查；环境未安装 Bun）
- `PLAYWRIGHT_BASE_URL=http://127.0.0.1:5173 npx playwright test tests/ads-workspace.spec.ts tests/ads-reporting.spec.ts --project=workspace --workers=2 --reporter=line`（3 passed）
- `npx @biomejs/biome check` 变更前端路径

未调用真实 TikTok/MCP、未写广告、未部署或推送。提交：`c058247`，提交主题 `ads: add six-dimension reporting workspace`。
