# AI 投放工作台 v1 运行与接入

适用于 `codex/ai-ad-workbench-v1` 的正式 React/FastAPI 实现。产品允许每租户多位优化师，每位绑定单一投放策略版本与多条已有自动规则。自动选剧、复制放量、多方案及 A/B 不在 v1 范围。

## 应用与数据库

本版本迁移 head 为 `0127_ai_knowledge`。新增优化师时区/复盘时间、素材配置及独立素材订单台账，不可作为无迁移快修直接发布。部署按主部署手册选择数据库迁移方案、正常停止写入和排空；本文不表示已取得生产发布或真实投放授权。

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

凭据至少 32 字节随机值，与浏览器登录、TikTok OAuth 和素材推送 Key 分离。不要在日志、命令行参数或源码中填写值。在 DSH 侧将相同凭据放入仅服务用户可读的 `0600` 文件，并注册本仓库 `integrations/dsh-ai-workbench` 原生插件。插件使用 DSH `0.2.2-rc.1` 的 agent、llm、session、tools、skill、scope 接口；依赖由 DSH 宿主提供，不修改 DSH 核心，也不直接复用旧本机投手服务。

插件配置对象（按宿主现有插件注册机制加载）：

```json
{
  "url": "https://<本环境 TK-ADA 域名>",
  "tokenFile": "/run/secrets/tkada-ai-review-token",
  "stateRoot": "/var/lib/dsh/tkada-reviews"
}
```

必须配置 DSH 默认模型；也可通过插件 `modelSelection` 显式选择已配置的模型。`url` 只允许 HTTPS 纯 origin；本机隔离验收可用 `http://127.0.0.1:<端口>`。生产域名、token 文件和状态目录由部署方明确提供，不能推断或复制测试凭据。

运行链路为：用户手动发起或 Beat 创建复盘 → TK-ADA 冻结优化师、策略、规则、计划、搭建、报表、知识和历史 → 插件领取并首次绑定方法包 → 返回五分钟任务级 JWT → 原生 DSH 独立会话通过 `skill` 读取主方法及相关专项 → `tkada_complete_review` 提交结构化结果 → TK-ADA 校验及发布。领取服务凭据不进入模型提示；会话仅开放受限 `skill`、`tkada_search_knowledge`、`tkada_read_knowledge` 和 `tkada_complete_review`，不能执行 shell、读取凭据或调用平台。每任务目录按租户/优化师/任务/fence 分隔。

### 方法、插件与领域系统边界（2026-11-13）

- DSH 宿主负责模型、Agent 会话和原生 SkillRegistry。无需修改或嵌入 DSH 核心。
- 插件 `integrations/dsh-ai-workbench/skills/ai-delivery-review/` 是主 Skill 和五个专项方法的唯一源。插件按任务 scope 注册方法，通过受限的同名 `skill` 工具调用原生 registry，隔离宿主个人 Skill 和其他任务的方法。初始提示仅提供方法目录；主方法必须加载后才能正常完成，专项按需加载，结果记录 `method_usage`。
- TK-ADA 负责数据、知识/经验、历史反馈、授权、参数 Schema、提案校验和正式发布。业务后端不再读取或维护方法文件；任务中保存的正文/版本/哈希是插件首次绑定的归档副本，重试不随插件升级改变。
- 创建时业务输入上限 216,000 字节，为方法归档预留 96,000 字节、按需知识正文及元数据预留 200,000 字节，总上限 512,000 字节。领取时只绑定方法，不能再次截断已冻结业务输入。`input_budget` 描述业务输入部分；方法归档有独立容量校验。
- `/claim` 请求体现在必须是 `{main, references}` 方法清单，每项为 `{key, description, version, sha256, body}`。`main.key=ai-delivery-review`，references 固定 builds/rules/materials/business/scale，正文 UTF-8 哈希经服务端校验。插件不复制策略、规则执行器或知识数据库。

升级时 API 与插件 `1.1.0` 必须配套更新；旧版空请求体领取不再支持。切换前取消仍在运行或已冻结旧格式方法的待处理复盘，再重新发起新任务；历史已完成结果保留。这个边界调整无新增迁移，但首次交付知识库仍需此前的 `0127_ai_knowledge` 迁移。这里是发布准备说明，本轮未部署。

开发方法复用指南：[从业务工具到 AI 工作台](https://gz-junbo.feishu.cn/docx/BWDudD226o6A5cxEjvscVVOZnSh)。

机器端点为 `POST /api/integrations/ai-workbench/reviews/claim`，及任务级 `/{task_id}/heartbeat`、`/{task_id}/complete`。服务身份不能直接发布配置；任务 JWT 固定租户、优化师、任务、fence、audience 和有效期。每次完成/心跳重新核验成员权限与优化师状态。取消/重启立即使旧任务无法回传生效；原生插件心跳失败后中止模型会话。超期 RUNNING 可由新 fence 恢复，同一优化师复盘串行，等待模型不持有数据库事务或业务 Worker。

人工模式的全部提案逐条待确认；拒绝不改版本。自动模式只发布已授权字段与数值范围内的变更，其余留待人工审阅。预算/消耗/ROAS 使用绝对调整上限，搭建结构与规则条件/动作/时间分别授权。人工采纳是对当前具体变更的确认，仍受租户角色、账户交集、不可变基线、共享实体保护及现有策略/规则 Schema 约束，不扩大长期自动授权。金融数值须用十进制字符串。

## 验收边界

本地真实 PostgreSQL/Redis 验证与外部传输模拟分开记录，见 [v1 验收记录](../validation/2026-11-12-ai-workbench-v1.md)。没有配置真实模型、公司 Key、版权方和 TikTok 资产时，不把协议测试或本地页面显示视为真实下单、广告搭建、模型复盘或经营效果证明。


### Agent 主动知识检索（插件 1.2.0）

知识仍由 TK-ADA PostgreSQL 的版本表管理。创建复盘时只提供最多 20 条知识元数据目录，不注入整批正文；目录不完整也不限制后续搜索。插件提供两个任务级只读工具：

- `tkada_search_knowledge({query, offset?})` → `POST /api/integrations/ai-workbench/reviews/{task_id}/knowledge/search`。按标题、正文、适用条件做转义后的关键词子串匹配（空格分词，任一匹配），每页 10 条，返回 `next_offset`，不返回正文；不是语义检索，也不自动获取外部最新政策。
- `tkada_read_knowledge({knowledge_id})` → 同前缀的 `/knowledge/read`。读取被允许的具体版本，返回正文、来源、适用条件、有效期、验证状态、冲突提示及哈希，并归档到当前任务的 `evidence.knowledge`。重复读取返回同一副本，引用 ID 可用于提案、分析建议和经验候选。

两者只使用该任务短令牌，服务领取密钥不进入工具或模型。每次请求重新核验角色、任务 fence/有效期、运行状态、优化师版本及账户授权；任务行锁保护并发正文归档。任务完成、取消或范围改变后不能继续读取。

检索的知识版本截至任务 `captured_at`：公共知识 + 本租户知识 + 当前 BC 案例，排除候选/退休版本。截止后的更新只在下一轮使用，历史任务不会漂移。政策的业务生效日期需结合正文适用条件核验；`created_at` 是知识版本发布时刻，不代表政策生效日期。过期/来源未核实的条目标为背景，不能当作自动调整或到账依据。

读取每轮最多 20 条、正文 32,000 字符，总 JSON 仍受 512,000 字节限制；超额明确返回预算错误，不截断已冻结源数据。可以新增 `policy`、`experience` 标签，未引入向量库、对话录入或新的数据库迁移。实际赔付/补贴政策须通过知识管理接口录入和审核，接通检索不等于已有完整政策库。

部署需 API 与插件 1.2.0 配套更新；旧格式待处理任务按前述切换说明取消并重建。本轮只完成本地代码与协议验证，未部署或进行真实模型业务质量验收。

### 公共知识预置包导入

通过 `scripts/import-ai-knowledge-pack.py --help` 查看参数。管理员凭据只通过 `--token-file` 传入私有文件路径；远程 API 必须为 HTTPS 纯 origin，脚本拒绝重定向。导入会查询同 key 公共资产：相同正文/元数据跳过，变化则追加同资产版本；重复资产需人工整理。文件预置不代表已导入任何环境。

发布前阅读 [全分支上线审查](../validation/2026-11-13-ai-workbench-release-review.md)，其未完成业务项仍适用。
