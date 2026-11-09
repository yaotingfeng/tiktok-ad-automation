# Material Links Quantum Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans inline. Root explicitly prohibits child agents, commits and production writes; root provides final independent review.

**Goal:** 让一个已获许可的后台关联父在有界量子内推进最多3步，减少重复调度等待。

**Architecture:** dispatcher签发原成本ledger的3单位预留，consumer按实际持久预算裁剪。execute_job内部逐步提交但保持本代RUNNING，复用TransactionBoundaryReader围栏；最后沿既有outcome/refill归还一次。

**Tech Stack:** Python / SQLModel / PostgreSQL18 / Redis / Celery。

**Spec:** ../specs/2026-11-09-material-links-quantum-design.md

## Global Constraints

- 仅background material_links，最多3步/20秒；170秒reader、180秒Celery及240秒租约不扩大。
- max_steps=min(3,granted_budget)，旧grant1不可增权；消息/配额/公平权重/缓存/业务冻结不变。
- 每步独立commit；未知发送不退款；最终一次清租约/补位。
- 本地独占四个源文件及新增测试（第四个executions.py仅审查确认的中断传播分支）；禁止提交、生产操作、派生agent。

## Review Focus

- 新consumer收到旧grant1；新grant3由旧consumer执行1后费用闭合。
- 第二步失败不能丢前步已提交progress/费用。
- 网络间后继owner/gen/token/route/head变化不能接受旧返回或清新资格。
- 量子绝对期限不能在每次读取或事务重开。
- 缺/零预算、依赖等待、当前日与其他kind不得获得额外步骤。

## Task 1: 真实授权预算与量子边界（独立红绿）

Files: modify backend/app/modules/reporting/{jobs,job_tasks}.py and backend/app/modules/sync_control/dispatcher.py; create backend/tests/modules/reporting/test_material_links_quantum.py.

Interfaces: execute_job(..., max_steps: int=1, quantum_deadline: datetime|None=None) 保留默认单步；job_tasks在原RUNNING资格内读取主端点成本预算并传绝对截止；dispatcher为后台links预留3。

- [x] 写真实PG/Redis失败测试：grant3推进3步，每次下一HTTP可见前一步提交；grant1只能1；预算缺/零不发HTTP。
- [x] 运行新增测试确认旧源RED并保存私有日志。
- [x] 实现预算读取、20秒固定reader边界、execute_job逐步commit与最终outcome，保留真实计费。
- [x] 跑新增测试GREEN。

## Task 2: 恢复与混合版本（逐项红绿）

- [x] 扩展真实连接竞争/费用测试：第二步错误/owner/gen/token/route/head变化；deadline停止；依赖/COMPLETE/实时/非links原行为；单次outcome/refill；旧consumer只用1单位正常结算。
- [x] 失败先定位再最小修复；检查每次checkpoint的计数增量和冻结资格。
- [x] 跑新增回归及受影响report jobs/任务边界/成本与dispatcher测试，不重复无关大套。
- [x] Ruff/format、三源mypy旧新对照、diff检查；记录结果并交root独立review，不提交。

## Execution ledger

- 2026-11-09：spec/plan已根据已授权范围自行审阅；ledger.reserved_units同job/gen/physical唯一且首次签发后不重算，能够表达旧1/新3预算；此项已落实，见后续证据。

- 2026-11-09 Task 1：旧源4 RED/1 PASS；新首批5 PASS（3.63秒）。实际dispatcher签发3预算，旧grant1只能读1，缺/零预算规范释放且不打开reader。
- Task 2：新head与DomainError/Admission并发2 RED、缺预算与替换head1 RED、gateway入场与新head2 RED，均补原token复验；其它owner/gen/token/route、20秒截止、依赖/COMPLETE/当前日/非links、发送后软中断与实际费用、旧consumer退未用预留均已覆盖。
- 相关回归105 PASS/1 explicit transport skip（26.95秒）；此前jobs+成本55 PASS/1既有失败，HEAD三源shadow同失败为campaign_batch_still_truncated返回COMPLETE而旧测试期待STALE，未修改无关行为。
- 三源严格mypy旧新均129项既有错误，去行号消息Counter完全一致，无新增；Ruff/format/diff通过。全局mypy不宣称绿色。
- 最终24重点与显式SYNC_COST_TRACE=1传输基准全部通过：25 PASS（159.99秒）。此前显式基准skip已补验；不提交/不生产。独立审查由root负责。

- 最终独立审查两项Important均先真实复现：软中断与后继gen/token/owner并发3 RED；二次预算缺/零与同gen新head并发2 RED。根因分别为失租覆写中断和零步绕过head检查；最小修复后含head中断共30重点PASS（7.10秒），原execution boundaries 9 PASS（2.89秒）。独立审查V复核两修复无剩余Critical/Important。
- 最终四源mypy仍129项既有错误，去行号Counter与HEAD三源基线一致；executions.py无错误。不重复已过的160秒正常传输基准。
