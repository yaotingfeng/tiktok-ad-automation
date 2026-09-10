# 投放策略视觉试版 · 2026-09-10

## 授权与保留版本

用户反馈 dashboard-01 调整后信息分散，随后授权“先提交一版代码，然后再调整一版试下，方便回退”。本轮先确认主分支及 GitHub 均为 `4c732a20937a00ef18da04dde17d861baf9456d4`，创建并推送标记 `ui-before-refinement-20260910`，再从该提交建立 `feat/strategy-visual-refinement`。

只调整“投放策略”列表作为试版，等待实际阅读体验反馈后再决定其他页面。既有业务行为、API、权限、BC/租户范围、分页与操作入口保持原定义。主目录原有未跟踪 `docs/design-history/` 不纳入本轮提交。

## 实施

- 正文恢复唯一 22px h1；顶栏仅显示普通文字“投放工作”。标题及顶栏文字由页面自身的 React 生命周期管理。
- 内容区浅灰背景；筛选、表格和分页组合为一张 shadcn 白色 Card，分别使用 CardHeader、CardContent、CardFooter。
- 外距保持移动 16px、桌面 24px；Card 内同样保留 16/24px。表格边框与卡片边框之间有实际间距。
- 仅策略列表启用固定短列、名称弹性列和 1428px 最小表宽；窄屏横滚限制在表格内部。单元格上下 12px、左右 16px，长名称可查看完整 title，合法长预算/ROAS完整换行。
- “新建策略”为蓝色主操作。主题变量限定在策略页 DOM 标记内，背景由父内容区 `:has` 选择器应用，卸载该页即失效；共享组件默认外观及其他页面未全站替换。

## 验证记录

- TypeScript 与生产构建通过。Biome 检查修改的 TSX，通过；局部 CSS 经 Vite 构建与浏览器验证，不称其通过了被项目忽略的 CSS lint。
- 工作区外壳及租户/账户回归：93 passed，41.9s。全部使用现有 API 传输替身，不调用外部业务平台。
- 策略完整专项：36 passed，17.7s，覆盖搜索/分页、角色与失败路径、390/1024/1440/2304px几何、侧栏收起后短列不伸长、离开并返回列表时标题/主题恢复、长预算/ROAS文字范围不越界。旧几何断言按本轮已授权设计更新，其他业务断言保留。
- 实际 `http://127.0.0.1:8011` 使用当前工作树生产产物更新；在已有会话、已有租户与策略上截图核对。1920px下正文标题22px、Card外距24px、内距24px，表格短列实测为96/168/112/104/108/88/208/324px。进入账户页后仅有16px顶栏标题、浅灰背景撤回、主色恢复，回到策略页试版恢复。未保存业务表单。

复现命令（先在 frontend 启动 Vite，再分别运行）：

```sh
bun run dev --host 127.0.0.1 --port 5195 --strictPort
PLAYWRIGHT_BASE_URL=http://127.0.0.1:5195 bunx playwright test tests/strategies.spec.ts --project=workspace --workers=4 --reporter=line
PLAYWRIGHT_BASE_URL=http://127.0.0.1:5195 bunx playwright test tests/workspace-shell.spec.ts tests/tenants-accounts.spec.ts --project=workspace --workers=4 --reporter=line
bun run build
```

93项默认工作区兼容检查在列宽修正前完成，随后36项策略及生产构建在最终修正后完成；默认表格未开启新布局。不是本轮重跑全站317项，也不把功能与几何通过当成视觉体验已经获用户认可。

独立评审发现并修复：TanStack 的 resolved columnDef 会合入默认 size，必须显式指定弹性列；固定列中合法大数需要换行以避免覆盖下一列。对应几何断言验证真实尺寸，不再沿用上一版“卡片必须移除”的设计假设。

## 回退

本轮源码、验证和记录形成独立提交，未改写既有历史。需要撤回时可对本轮提交执行 `git revert` 后重建前端；旧版代码始终可通过已推送的 `ui-before-refinement-20260910` 查看。标签不是数据库备份，本次未修改数据库、凭据或外部广告。
