# AI 投放工作台 v1 运行与接入

适用于 `codex/ai-ad-workbench-v1` 的正式 React/FastAPI 实现。产品允许每租户多位优化师，每位绑定单一投放策略版本与多条已有自动规则。自动选剧、复制放量、多方案及 A/B 不在 v1 范围。

## 应用与数据库

本版本迁移 head 为 `0126_ai_plan_retry_fairness`。新增优化师时区/复盘时间、素材配置及独立素材订单台账，不可作为无迁移快修直接发布。部署按主部署手册选择数据库迁移方案、正常停止写入和排空；本文不表示已取得生产发布或真实投放授权。

Beat 的 `ai_workbench.dispatch_due_plans` 每 30 秒领取到期剧单，搭建交给 `builds` 队列；`ai_workbench.scan_material_orders` 每 30 秒交接/查询素材订单，订单任务在 `control`；`ai_workbench.schedule_reviews` 每 60 秒检查当地复盘时间。Worker 继续沿用现有队列，无新增常驻 Python Worker 或部署级布尔开关。

排期默认 `Asia/Shanghai`，可按优化师设置 IANA 时区。用户输入是当地墙上时间，台账转换为 UTC。未来排期不提前冻结；发生记录或素材订单创建后，行内容只读，修改其他行保留原行 ID。当天截止后不自动跨日补搭建，未知平台结果不重建。

创建、准备、预览、提交和回读继续走既有搭建服务。准备尚未就绪时等待并按原草稿/请求号恢复，不把正常异步等待记成永久失败。优化师暂停同时约束已投递的搭建任务；恢复后仍沿用原发生记录。

## 素材订单

先通过既有素材制作开户流程为租户建立 `ready` 绑定，服务端使用该租户加密保存的公司 `owk_` Key；浏览器和 DSH 都不会收到公司 Key。

在优化师编辑页填写本租户已启用的素材制作策略 ID 和飞书回写表格链接/token。`GET /api/tenants/{tenant_id}/ai-material-options` 提供租户策略目录。剧单勾选“下单素材”只保存意图；缺配置时不会猜测策略或发单。配置齐全后 Beat 提前处理已保存的未过期剧单，不必等到搭建时间。

v1 使用文档约定的素材库工作流：`GET /api/strategy-catalog`、`GET /api/material-library-production/capabilities`、`POST /api/schedules`、`GET /api/jobs/{job_id}`。不传集数字段，在整剧与默认集数能力下均遵守上游合同。成片推送仍复用现有素材回传/入库，搭建阶段按剧匹配，不将勾选下单等同于素材已匹配。

订单在外部请求前持久化冻结请求体与 `startTime`，幂等键为 `tkada-ai:<订单 UUID>`。超时/未知按上游同键同体幂等合同恢复；拿到 job ID 后只查原 job，不再提交新单。逐剧状态及 delivery 记录保留在日报证据中。日报明确区分意图、获得 job ID 的已下单、等待、未知、失败和实际搭建匹配。

## 独立 DSH 接入

部署方在 TK-ADA API/Worker/Beat 共用运行 Secret 中增加 `AI_DSH_SERVICE_TOKEN`，为空时不接受机器领取、不启动自动复盘，前端显示未配置。此项是服务凭据，不是业务功能开关。普通已有四开关保持原值；首次配置须核对目标、范围和实际加载值，不能将本地模板当作服务器已启用。

凭据至少 32 字节随机值，与浏览器登录、TikTok OAuth 和素材推送 Key 分离。不要在日志、命令行参数或源码中填写值。在 DSH 侧将相同凭据放入仅服务用户可读的 `0600` 文件，并注册本仓库 `integrations/dsh-ai-workbench` 原生插件。插件使用 DSH `0.2.2-rc.1` 的 agent、llm、session、tools 接口；依赖由 DSH 宿主提供，不修改 DSH 核心，也不直接复用旧本机投手服务。

插件配置对象（按宿主现有插件注册机制加载）：

```json
{
  "url": "https://<本环境 TK-ADA 域名>",
  "tokenFile": "/run/secrets/tkada-ai-review-token",
  "stateRoot": "/var/lib/dsh/tkada-reviews"
}
```

必须配置 DSH 默认模型；也可通过插件 `modelSelection` 显式选择已配置的模型。`url` 只允许 HTTPS 纯 origin；本机隔离验收可用 `http://127.0.0.1:<端口>`。生产域名、token 文件和状态目录由部署方明确提供，不能推断或复制测试凭据。

运行链路为：用户手动发起或 Beat 创建复盘 → 冻结优化师、策略、规则、计划、搭建、报表和证据 → DSH 服务领取 → 返回五分钟任务级 JWT → 原生 DSH 独立会话完成结构化提案 → TK-ADA 校验及发布。领取服务凭据不进入模型提示；会话只开放 `tkada_complete_review`，不能执行 shell、读取凭据或调用平台。每任务目录按租户/优化师/任务/fence 分隔。

机器端点为 `POST /api/integrations/ai-workbench/reviews/claim`，及任务级 `/{task_id}/heartbeat`、`/{task_id}/complete`。服务身份不能直接发布配置；任务 JWT 固定租户、优化师、任务、fence、audience 和有效期。每次完成/心跳重新核验成员权限与优化师状态。取消/重启立即使旧任务无法回传生效；原生插件心跳失败后中止模型会话。超期 RUNNING 可由新 fence 恢复，同一优化师复盘串行，等待模型不持有数据库事务或业务 Worker。

人工模式的全部提案逐条待确认；拒绝不改版本。自动模式只发布已授权字段与数值范围内的变更，其余留待人工审阅。预算/消耗/ROAS 使用绝对调整上限，搭建结构与规则条件/动作/时间分别授权。人工采纳是对当前具体变更的确认，仍受租户角色、账户交集、不可变基线、共享实体保护及现有策略/规则 Schema 约束，不扩大长期自动授权。金融数值须用十进制字符串。

## 验收边界

本地真实 PostgreSQL/Redis 验证与外部传输模拟分开记录，见 [v1 验收记录](../validation/2026-11-12-ai-workbench-v1.md)。没有配置真实模型、公司 Key、版权方和 TikTok 资产时，不把协议测试或本地页面显示视为真实下单、广告搭建、模型复盘或经营效果证明。
