# 引导式体验版实施计划

设计：`docs/superpowers/specs/2026-10-21-guided-demo-mode-design.md`。用户已授权由实施者决定细节，完成计划后直接实施。

## 技术决定

- **假后端**：不用 MSW，直接给生成的 axios 客户端换 `adapter`（`client.setConfig({ adapter })`）。所有 SDK 调用都在进程内处理，没有 Service Worker 作用域和 `/demo/` 子路径的问题，也不需要新依赖。`baseURL` 设为 `demo://tk-ada`，这个协议浏览器不认，任何漏网请求都发不出去。
- **兜底拦截**：体验版入口替换 `window.fetch` 和 `XMLHttpRequest.open`。只放行假后端签发的 `https://demo-storage.invalid/` 直传地址（交给假后端处理），其余一律报错并在页面提示。
- **存储隔离**：体验版入口在加载应用代码之前，把 `window.localStorage`/`sessionStorage` 替换成内存存储，生产键一个都读不到、写不到，刷新即重置。
- **整页跳转**：新增 `src/lib/browser-navigation.ts`，所有 `window.location.assign` 都改走它。生产行为不变；体验版替换实现：站内路径转为 hash 路由跳转，TikTok 授权等外部地址由各领域注册的模拟回调处理。
- **路由**：体验版用 `createHashHistory()`；生产仍用浏览器历史。应用装配从 `main.tsx` 抽到 `src/app.tsx`，生产入口和体验版入口共用。
- **构建**：`vite.demo.config.ts`，`root: demo/`、`base: /demo/`，产物输出到 `backend/app/frontend/demo/`。`bun run build` 依次构建生产版和体验版，Dockerfile/CI 无需改动。生产入口不引用 `src/demo/**`。
- **操作表**：`scripts/generate-demo-operations.ts` 从 `openapi.json` 生成 `src/demo/operations.gen.ts`（operationId → 方法、路径模板、请求/响应类型），处理函数按 operationId 注册并获得类型检查。
- **覆盖检查**：`scripts/check-demo-coverage.ts` 比对 openapi 操作和已注册处理函数，豁免清单写在 `src/demo/coverage-exemptions.ts` 并注明原因；接入 `deploy/ci/check.sh`。
- **引导**：driver.js，主线引导 + 每页“本页怎么用”，元素用 `data-demo-tour` 之外优先用稳定的可见文本/role 定位。体验版外壳（横幅、重置、引导入口、一键登录）放在 `src/demo/shell/`。

## 阶段

1. 基础设施：导航抽象、`app.tsx`、体验版入口与隔离、适配器与注册表、操作表生成、构建配置、覆盖检查、共享演示世界数据（租户、BC、账户、剧、素材、用户）、登录/用户/租户处理。
2. 各领域假后端与数据（并行）：账户与授权；版权方；素材与上传、推送、制作；策略与搭建；广告报表与广告管理；自动化规则、通知。
3. 体验版外壳与引导。
4. 平台租户管理页的“打开体验版”按钮；端到端测试（主线跑通、全程无 `/api/` 请求、生产存储键未被触碰、每页无未覆盖报错）；构建与覆盖检查；记录进度。

## 验收

- `bun run build` 产出 `backend/app/frontend/demo/index.html`，生产产物中搜不到体验版标识。
- `bun scripts/check-demo-coverage.ts` 通过。
- 体验版 Playwright 测试通过。
- 后端以 `/demo/` 返回体验版 `index.html`。
