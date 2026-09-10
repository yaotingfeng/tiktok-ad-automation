# 全工作区视觉统一实施计划

> **For agentic workers:** 按用户已授权的当前会话并行实施，根端集成与验收；不另行开启执行方式确认。

**Goal:** 将已确认的策略页布局推广到所有页面，主按钮恢复原黑色。

**Architecture:** 保留现有 shadcn/Radix 组件与业务结构，由共享 Shell、正文标题、Card 与 Table 统一基础几何。各模块按业务区域组合卡片，表单、列表、详情与空状态分别适配；业务请求及权限不变。

**Tech Stack:** React、TanStack Router/Table、Tailwind v4、shadcn/ui、Bun、Playwright。

**Spec:** `docs/validation/2026-09-10-strategy-ui-refinement.md`；用户本轮确认“主按钮用原来的黑色，其他都没问题，全部页面都调整一下”。

## 全局约束

- 回退标记 `ui-before-workspace-rollout-20260910` 指向 `601c181`；原有未跟踪设计文件保留。
- 所有业务页唯一正文 h1 为 22px/32px/600；顶栏保留工作区上下文，不再把 h1 传送到顶栏。
- 内容区浅灰、卡片白色；页面及卡片水平间距移动 16px、lg 起 24px；表头左右16px，单元格上下12px/左右16px。
- 主按钮沿用全局中性 primary，删除策略试点的蓝色变量；深色模式继续使用已有中性 token 保持对比。
- 筛选、表格与分页属于同一白色 Card，表格独立边框与卡片外框之间有间距；弹窗不无限套卡，宽表只在表内滚动。
- 保留输入、筛选、游标分页、冻结/提交、上传并发与恢复、租户/BC范围、角色权限和外部接口契约。

## Task 1：共享基础与策略页面（根端）

Files: `features/workspace/{WorkspaceShell,WorkspacePageTitle,WorkspaceEmpty}.tsx`、`components/ui/{card,table}.tsx`、`features/strategies/**`、`components/Common/{AuthLayout,NotFound,ErrorComponent}.tsx`、`src/index.css`。

- [ ] 默认正文标题：`WorkspacePageTitle({ children })` 返回普通 h1；Shell 顶栏显示“平台管理”或“投放工作”，去掉共享 portal/context/state。
- [ ] Shell 内容区增加 `bg-muted rounded-b-[inherit]`，Card 基础响应式 padding；Table 基础 cell spacing；删除 strategy-list.css 与 import，策略页回归中性主题。
- [ ] 策略编辑保留左侧表单/右侧示例，使用正文标题与适当最大表单宽度；空状态合并重复提示，登录/异常页用同样的灰底白卡及黑色按钮。
- [ ] 更新 `tests/workspace-shell.spec.ts` 与 `tests/strategies.spec.ts` 的已被新设计替换的标题/主题断言，保留全部业务断言；确认列表到编辑再返回均22px正文及中性主题。

## Task 2：管理页面（管理代理）

Files: `features/tenants/{TenantAdminPage,MembersPage}.tsx`、`features/accounts/**`、`features/providers/**`、`routes/_layout/{admin,settings}.tsx`、`components/{Admin,UserSettings}/**`、`components/Common/DataTable.tsx`。

- [ ] 租户、成员、账户、用户列表按 `CardHeader(筛选) / CardContent(表格) / CardFooter(分页)` 组合。
- [ ] BC连接、版权方连接/结果与设置表单按业务分组；检查Sheet/Dialog与长标识、金额、权限缺失状态。
- [ ] 仅布局变更，运行所改 TSX 的 Biome 与 diff 检查并独立提交；根端集成后跑租户/账户/版权方完整行为回归。

## Task 3：广告搭建与任务（搭建代理）

Files: `features/builds/**`。

- [ ] 输入、准备、冻结预览、任务列表与详情统一正文页头；筛选/Tab内表格/分页有同一业务卡片归属。
- [ ] 保留冻结数据、所有操作入口、步骤条、底部sticky提交、固定首列和表内滚动；已有表单卡片避免重复包裹。
- [ ] scoped Biome、diff 检查并独立提交；根端跑搭建准备/预览/任务原回归。

## Task 4：素材工作区（素材代理）

Files: `features/materials/**`（仅展示层）。

- [ ] 素材Tab、上传Tab、批次记录、预算/容量说明与队列分组；筛选/表格/分页不贴卡片外框。
- [ ] Sheet/预览/上传恢复弹窗保持足够内距；不修改导入hook、上传调度和恢复API。
- [ ] scoped Biome、diff 检查并独立提交；根端跑素材/R2/上传原回归。

## Task 5：集成验收与交付（根端）

- [ ] 审阅各提交 diff 后顺序集成，在frontend运行 `bun run build`。
- [ ] Vite开发服务上跑 `PLAYWRIGHT_BASE_URL=http://127.0.0.1:5196 bunx playwright test --project=workspace --workers=4 --reporter=line`，包含真实行为与边界fixture；验证各类页面390/1024/1440/2304宽度、正文唯一标题、中性主色、卡片表格间距。
- [ ] 使用实际8011页面已有会话检查平台/租户管理、策略列表和编辑、版权方、素材/搭建缺BC状态与菜单跳转；有BC的完整业务页用传输替身验证，不冒充真实授权。
- [ ] 更新本计划勾选及 `docs/implementation-progress.md`、验收记录，提交并push独立分支和主开发分支，保留已有checkpoint；本地生产静态资源与最终提交一致。
