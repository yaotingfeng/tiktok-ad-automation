# 搭建任务详情信息层级优化

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

本次无数据库迁移、无功能开关变更；增加两个只读响应的字段，发布时前后端须配套更新。未调用真实 TikTok 写入、未修改授权或历史广告对象。当前仅本地实现、验证和提交，尚未推送或部署。

聚焦提交主题：`builds: simplify task details and make submitted previews read-only`。
