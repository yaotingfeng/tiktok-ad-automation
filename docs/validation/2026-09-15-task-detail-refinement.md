# 搭建任务详情信息层级优化

## 用户复验后的修正

- 只读查看当前测试站任务详情，确认完成任务出现「任务连接需要检查」，且信息散落在页头和独立摘要行，统计卡片留白偏多。
- 根因：`submissions.py` 的 `recovery_mode=BLOCKED` 代表既不可原授权核查、也不可新授权历史核查。`recovery_summary` 在没有候选时返回 `recovery_no_candidates`，正常完成任务也满足此条件；它不是连接健康状态。仅能重试而无需核查的任务也可能返回 BLOCKED。
- 删除从 `BLOCKED` 或缺少历史路线推断当前授权失效的通用告警；保留恢复操作组件根据 `recovery.reasons` 显示的具体阻断、未知请求和回执。「查看连接与授权」放入技术详情，租户与 BC 参数不变。未改变后端权限或恢复规则。
- 延续现有 shadcn 中性灰底白卡：任务状态与标题同行、常用操作统一按钮样式；版权方/策略/预算并入任务概览，三级计数改为紧凑分隔行。移动端三个计数统一上下对齐，技术详情移至概览卡右上，主表更靠近首屏。
- 新增 BLOCKED 无待办、可重试、真实权限不足回归，以及 1920/1440/390px 长账户名称布局与计数对齐检查。测试均使用合成数据，不调用真实广告或授权服务。
- 最终任务页/提交配置/双通道页面回归 **87 通过（29.6 秒）**；TypeScript/Vite 构建、3 文件 Biome、`git diff --check` 通过。首轮 86 通过/1 失败仅为新增授权链接断言依赖查询参数顺序，改为分别核对路径、租户及 BC/tab 参数后重跑全套通过。人工检查桌面及手机截图，未将截图夹具视为线上数据；原任务列表重复 `all` 键警告仍存在。
- 本轮修正已随固定提交 `c6044077e63e7a4c117c6aea9b4c1723788fba54` 推送并部署到新加坡测试环境。

## 已确认的调整

用户在查看任务详情后确认优化：创建结果和待处理事项优先，合并重复状态与 BC 信息，连接、完整编号和执行统计按需展开，历史提交配置只读。

- 任务信息合并到标题和紧凑摘要；展示 BC 名称、提交人、时间、版权方、策略及原精度配置预算。
- 创建结果默认展示广告系列、广告组、广告三项已创建/已提交数量。失败、待核实与非广告依赖异常保持显式展示；完整分项统计和执行阶段折叠。
- 正常任务移除独立执行连接卡、账户授权快捷按钮、完成提示和无恢复候选提示。连接受阻时仍显示授权检查入口；未知恢复请求和已受理回执继续保留。
- 明细默认展示剧目、账户、素材准备、创建数量与结果。广告系列完整名称、账户/远端编号及平台状态移到「查看明细」侧栏；原素材组、SP 创意和原件预览保持按需查询。
- 「查看冻结预览」改为「查看提交配置」。服务端 `PreviewSummary.submission_id` 根据同租户、同 BC、同预览的持久化任务返回；跨会话访问历史配置均不出现创建、调整、修正按钮及创建步骤条。
- 操作记录补充剧目与账户定位，中文说明执行事件；原事件码、尝试序号、步骤/证据编号折叠保留。`REQUEST_ARMED` 仅说明进入发送阶段，`READBACK_PAGE` 仅说明读取一页，不能冒充已创建或核查通过。
- 使用现有 shadcn/Radix Card、Badge、Tabs、ManagementSheet 和语义色，保持统一标题、灰底白卡与中性按钮规范。

## 验证

- `frontend`: `bunx playwright test tests/build-task-pages.spec.ts tests/build-preview.spec.ts tests/build-channels.spec.ts --project=workspace --workers=4 --reporter=line`：最终 **84 通过**。首轮唯一失败为广告编号从主表移至详情后的旧定位，更新为打开详情核对编号后通过。
- 页面回归覆盖正常/失败/未知/补建任务、恢复请求幂等与丢失回执、viewer/403/401、租户/BC 切换、分页、预算精度、跨会话只读历史配置及桌面/手机布局。人工检查 1440/390px 完成任务、只读配置截图；桌面首屏可见明细。
- 独立临时 PostgreSQL（仅本机 25439 端口、`tkada_ui_test` 数据库）执行 `pytest tests/modules/builds/test_previews.py tests/modules/builds/test_submission_queries.py tests/modules/builds/test_submissions.py -q`：**43 通过**。补充日志定位字段断言后查询专项 **12 通过**（包含在上述范围内，不重复累加）。
- `bun run build`、本轮 10 个前端文件 Biome、5 个 Python 文件 Ruff、4 个服务端文件 mypy、`git diff --check` 通过；客户端类型通过项目生成脚本更新。
- 截图为传输边界测试数据，位于忽略的 `frontend/test-results/build-task-pages-完成任务-*/completed-*.png` 与 `frontend/test-results/build-preview-历史提交配置跨会话只读，viewer-*/submitted-config.png`；不代表线上业务验收。
- 任务列表原有日期筛选仍会输出重复 `all` 键警告；本次详情筛选中重复的全部选项已去除，未改动共享账户筛选组件。

## 发布范围

本次无数据库迁移、无功能开关变更；增加两个只读响应的字段，发布时前后端须配套更新。未调用真实 TikTok 写入、未修改授权或历史广告对象。

## 新加坡测试环境发布

- 2026-09-15 将 `c6044077e63e7a4c117c6aea9b4c1723788fba54` 从固定提交归档构建并发布，替换 `cc662011f0429b65949224f883497f0f830172d2`；服务器构建产物共 82 个前端静态文件。
- 切换前 Linux 隔离门禁 4 项及 MID/素材发现 24 项通过；API、Beat 停止接收新工作后，三个 Worker 正常排空。
- 完整备份 `/var/backups/tt-ada-staging/20260915T121541Z/` 的 PostgreSQL、Redis、项目、运行配置及私有配置校验全部通过；隔离恢复库迁移、队列隔离 2 项和 24 份加密响应归档恢复通过。
- 切换后 API、resources/builds/control Worker 与 Beat 均 active、零重启且工作目录指向新 release；数据库 current/head 均为 `build_batching`，功能开关和准入租约未变化。
- 平台管理员与租户管理员登录、租户权限隔离、MCP `READY`、HTTPS 页面和静态资源通过；三个 Celery 节点均 pong 且活动任务为空，五服务日志无 warning。未执行 TikTok 授权、素材上传或广告创建。

聚焦提交主题：`builds: simplify task details and make submitted previews read-only`。
