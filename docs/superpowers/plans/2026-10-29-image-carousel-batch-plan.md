# 图片轮播批量搭建实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 项目 AGENTS.md 的按需流程与用户后续指定的执行方式优先。

**Goal:** 在现有 TK-ADA 批量搭建流程中，一次配置图片规则，按剧生成轮播和配乐，再按原策略分配广告。

**Architecture:** 原图 → 有序创意单元 → 原广告组/广告规划器。批次默认、逐剧 patch 和手动保护分层；应用 generation 原子切换，冻结完整单元与音乐。每份草稿仅 VIDEO 或 CAROUSEL，单图属于 CAROUSEL。

**Tech Stack:** FastAPI、SQLModel/PostgreSQL、Celery/Redis、官方 TikTok SDK/MCP、React/TypeScript、shadcn/ui、Bun、pytest、Playwright。

**Spec:** [批量设计](../specs/2026-10-28-image-carousel-ads-design.md)。本计划取代旧的逐个配置主流程，用户授权的是设计和计划，所有实施项仍未执行。

## Global Constraints

- 图片 1–35 张/单元；视频一文件/单元；轮播 N 默认 3；无固定默认 music_id；一草稿一类。
- 现有 group/ad 两层分配保持；`creative_count` 是文案版本 K；`max_materials_per_ad` 是单元上限 M。场景能力限制仍优先。
- 所有剧目面向全部输入账户；tenant/BC/connection/binding generation 贯穿全链路。
- 不新增业务开关、不走自定义 TikTok HTTP 网关；音乐准备不在创建广告时运行。
- 旧冻结正文和哈希不可重写；旧可执行任务未排空不得迁移切换；无真实投放授权不得做广告写入。
- 正常产品仍是具体预览提交后 ENABLE；停用联调是独立授权测试，不新增产品激活审批流程。

## Review Focus

1. 数据分页导致自然排序错位或同名 tie-breaker 不稳定 → P2-T1 跨页确定性测试。
2. 手动换曲期间推荐迟到、应用期间规则变动 → P2-T2/T3 真实 PG/Redis 竞态测试。
3. 固定共享+文案复制令数量/预算倍增 → P3-T1 真实规划器和 UI 计数断言。
4. 某目标账户缺其中一张图 → P3-T3 断言整广告失败且正文不缩水，其他账户可继续。
5. 同一图片复用于多轮播，报表连接放大金额 → P3-T4 关联去重/粒度测试。

## 阶段与依赖

| 顺序 | 计划 | 前置与退出条件 |
|---|---|---|
| P1 | [图片与音乐基础](2026-10-29-image-carousel-p1-assets.md) | 完成官方合同核对；图片批量导入与目标映射可验证 |
| P2 | [批量编排与配乐](2026-10-29-image-carousel-p2-composition.md) | P1 合同稳定；整批生成/重生成与复用可以独立演示 |
| P3 | [冻结、执行与最小报表](2026-10-29-image-carousel-p3-delivery.md) | P2 单元稳定；多单元真实创建回读、迁移与视频回归通过 |

P1/P2 是独立可测的软件增量，保留在本地集成并按职责提交；面向投手的发布以 P3 为一个完整交付，避免出现能选图片却不能发布的入口。各阶段分别评审迁移，集成发布统一排空/备份/升级，不把三次生产迁移当作默认安排。

## 开始实施前

- [ ] 读取本 spec、阶段计划与 AGENTS.md；记录 `git status -sb`、提交号、Alembic head；保留当前其它任务未提交修改。不创建新分支、不自动推送。
- [ ] 读取 config/README.md，确认本地 PG/Redis 与测试配置。按仓库测试启动方式配置，不输出真实环境文件内容。
- [ ] P1-T1 核对官方图片上传/推荐合同与双通道能力，验证条件记录在计划指定的验证文档中。
- [ ] 锁定第一轮实施文件所有权；当前 api.py、mini_selection.py、客户端生成文件等存在其它任务改动，编辑前重新核实，不能覆盖。

## 统一任务节奏与提交

每个阶段任务先新增指定行为测试并观察预期失败，再实现明确接口，再跑任务定向测试；通过后检查语法/格式并做聚焦提交。文档写出的命令是未来验收指令，不是本轮已通过记录。

所有后端命令从 `backend/` 执行，使用 `uv run pytest ...`。PG/Redis 并发与迁移用真实本地服务；平台模拟仅在传输边界。前端从 `frontend/` 执行定向 `bunx playwright test ...`，遵循现有 Playwright 项目配置。集成根目录 `bun run --filter frontend build`；后端最终 `uv run ruff check app tests`、`uv run ruff format --check app tests`、`uv run ty check app`。如已有无关失败，应基线对照记录，不能记成通过。

提交前执行 `git status -sb`、`git rev-parse --show-toplevel`、`git diff --check`；只暂存该任务明确路径，审查 `git diff --cached`，再 `git commit -m '<范围>: <用户可见行为>'`。禁止 `git add .`；不自动推送。更新 docs/implementation-progress.md 的任务、验证与提交记录。

## 完成矩阵

| 设计需求 | 实施任务 |
|---|---|
| 手动批量图片上传/真实类型/主图分发 | P1-T1/T2/T3 |
| 推荐、搜索、按 ID 音乐读取及草稿范围 API | P1-T4、P2-T3 |
| 批次默认、逐剧覆盖、自然排序、尾图策略 | P2-T1 |
| 两种保护、差异试算、幂等/原子应用 | P2-T2 |
| 手工增删/排序、复制/合并/拆分及恢复候选 | P2-T2/T5 |
| 配乐批处理、请求栅栏与进度 | P2-T3 |
| 可复用轮播与原图引用保留 | P2-T4 |
| 批量界面、例外与类型选择 | P2-T5 |
| 原结构计数、场景格式、50 与预算 | P3-T1 |
| 有序冻结、严格编译及回读 | P3-T2 |
| 缺图/缺曲账户失败、UNKNOWN 恢复 | P3-T3 |
| 广告报表最小支持、素材规则边界 | P3-T4 |
| 存量视频、历史冻结、部署、真实验收 | P3-T5 |

## 上线后单独工作

细粒度轮播素材报表和图片素材自动控损依赖只读平台证据，另立任务：先确认 ad/creative/image 粒度及收入归属，再设计字段与规则阈值。不得按图均分、重复归因，或自动继承视频素材阈值。现阶段提供广告级指标与明确的格式/支持范围。
