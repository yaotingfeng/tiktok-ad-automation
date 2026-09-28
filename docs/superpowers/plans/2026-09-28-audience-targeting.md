# 受众定向 Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. 已确认方案由当前会话直接实施；不重复请求功能授权。

**Goal:** 策略预设国家、语言、年龄和性别；搭建按当前账户和 Minis 的共同范围核验并冻结严格定向。

**Architecture:** 复用场景事实、草稿版本、预览快照以及 SDK/MCP 共用编译器。新增独立定向合同与只读地区目录；仅定向修改保留素材分组，执行只用冻结设置。

**Tech Stack:** Python/Pydantic/SQLModel/PostgreSQL/Alembic；React/TanStack/shadcn/ui；pytest/Playwright。

**Spec:** ../specs/2026-09-28-audience-targeting-design.md

## Global Constraints

- 国家目录来自当前明确 BC 内有效场景证据，不使用全球国家全集。
- 用户已确认严格限制，不实现受众建议模式，不自动删除无效国家或放宽条件。
- 单批统一定向，ALL_AVAILABLE 解析为所有所选账户共同可投国家。
- 旧冻结请求保持原语义；不调用真实广告写接口，不部署或推送。
- 使用现有当前分支，保留无关改动。相关测试使用本轮独立本地测试数据库。

## Review Focus

- 未知、过期、撤权或不同授权代数的账户不能缩小交集分母而伪造可用目录。
- 仅定向修改必须保留手动素材组并使旧预览失效，重复请求不得二次更新。
- 预览、两通道发送与回读不能丢失年龄/语言/性别/模式；缺字段不补请求值。
- 新设置对旧策略和历史冻结任务的影响必须分别验证。
- 搜索无结果、旧选择失效、未选 BC 和只读角色必须有可理解页面反馈。

## Task 1 — 平台合同与领域模型

- [x] 核实官方 Minis 定向模式、语言代码与回读字段，记录适用范围与局限。
- [x] 在 `backend/tests/modules/builds/test_targeting.py` 写国家/年龄/语言/模式和集合语义的失败测试并运行。
- [x] 新建 `backend/app/modules/builds/targeting_schemas.py` 与纯定向解析模块，扩展 `StrategyConfig`、草稿字段和 Alembic 迁移。
- [x] 验证默认、指定国家非空、未知字段拒绝、非法枚举及序列化行为。

## Task 2 — 目录、保存和预览

- [x] 写 PostgreSQL 行为测试：场景地区交集、BC 隔离、过期、账户缺失、草稿覆盖/恢复、幂等及素材组保留。
- [x] 新建定向目录/保存服务，扩展策略与搭建 API；目录集合查询有界返回，账户差异分页。
- [x] 将有效定向和共同国家纳入预览冻结与摘要，逐账户核实实际地区 ID，失败不放宽条件。
- [x] 运行领域与草稿/预览相关测试。

## Task 3 — 创建与回读

- [x] 写 SDK/MCP 参数及读回差异测试，确认新增字段目前不能表达。
- [x] 扩展 `contracts/builds.py`、`request_compiler.py` 和 `readback_compare.py`，统一编码与规范化集合。
- [x] 验证非法模式组合、缺字段、字段不一致、顺序变化以及历史未设置字段。

## Task 4 — 页面及验收

- [x] 生成客户端；新建共享定向表单和摘要，策略页面使用 BC 有效目录，准备页使用本批交集。
- [x] 接入保存、恢复策略默认、账户差异、无目录/无交集/不可用选择及预览展示。
- [x] 添加 Playwright 行为用例，验证选择保存、无效国家保留、只读权限及预览摘要。
- [x] 执行相关 pytest、Playwright、类型检查、前端构建和差异检查；完成独立只读审查并修复问题。
- [x] 更新实施进度和验证证据，仅暂存本轮改动后本地提交。


## 验证结果（2026-09-28）

- PostgreSQL/Redis 本地离线回归：173 项通过；追加跨 BC 场景隔离测试 1 项通过，共 174 项。
- Playwright 策略与搭建页面 113 项通过；追加只读定向及冻结摘要 2 项通过，共 115 项。
- 修改范围 Ruff、ty、Biome、TypeScript/Vite 构建与 `git diff --check` 通过；专用测试数据库 Alembic head 为 `audience_targeting`，`alembic check` 无新增差异。
- 独立只读审查发现并修复：执行场景未应用冻结定向、目录缺授权证明条件、已核实空交集误判为待核实；复审无新增阻断项。
- 官方传输使用离线替身，未进行真实广告写入或服务器部署。详细命令与交付边界见实施进度记录。
