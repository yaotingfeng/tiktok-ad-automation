# 联调、容量与发布验收 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 按用户偏好按需选择执行方式。

**Goal:** 验证跨模块搭建结果、租户隔离、故障恢复及大量账户处理，并记录真实 SDK 联调与运行环境的交付证据。

**Architecture:** 先在独立 PostgreSQL/Redis 与严格 SDK 替身下完成全流程和故障测试，再在实际授权环境中验证权限、资产与创建结果。业务验收通过产品预览与提交接口完成，不新增独立激活中心或自研 API 网关。

**Tech Stack:** pytest、Playwright、PostgreSQL、Redis/Celery、Docker Compose、官方 TikTok Python SDK、Python 标准库。

**Spec:** [整体设计](../specs/2026-09-08-tiktok-00-overall-design.md)、[账户](../specs/2026-09-08-tiktok-01-tenants-accounts-design.md)、[版权方](../specs/2026-09-08-tiktok-02-provider-links-design.md)、[素材](../specs/2026-09-08-tiktok-03-materials-design.md)、[策略](../specs/2026-09-08-tiktok-04-strategies-design.md)、[搭建执行](../specs/2026-09-08-tiktok-05-build-execution-design.md)。

## Global Constraints

- 接入 BC 下全部有权限接入的账户，按大量账户设计。
- Campaign、Ad Group、Ad 创建时直接使用 ENABLE。
- 保留成功对象，只补缺失步骤；创建结果未知时先回读核实。
- 租户内不按投手划分账户范围；跨租户资源和凭据始终隔离。
- 素材上传不识别或绑定剧目；搭建时按文件名包含完整剧名匹配。
- 全部剧目使用同一批粘贴账户；不出现滑动轮转或二次账户勾选。
- 模拟通过不能代替真实 SDK 联调完成；本阶段不引入投放报表同步。

---

## 前置与文件范围

代码路径相对 `/Users/yaotingfeng/Documents/ytf/ytf-os-ad-skill/projects/tiktok-ad-automation`。P01～P06 的功能任务已通过各自检查后再运行本计划，不重复执行全部旧测试作为无目的的额外验收。

| 文件 | 职责 |
| --- | --- |
| `backend/tests/acceptance/{conftest,scenario}.py` | 两租户、多账户、两版权方及素材的固定场景 |
| `backend/tests/acceptance/test_batch_flow.py` | 准备、预览、直接启用创建的跨模块结果 |
| `backend/tests/acceptance/test_isolation_recovery.py` | 隔离、重复消息、未知结果和权限变更 |
| `backend/tests/fakes/tiktok.py` | 复用 P06 严格 SDK 替身，补足真实返回形状的场景 |
| `backend/tests/performance/test_large_batch.py` | 大目录、批量展开和稳定分页 |
| `backend/scripts/benchmark_batches.py` | 可重复运行的本地容量脚本 |
| `frontend/tests/acceptance-batch.spec.ts` | 批量输入、异常展示、预览编辑、提交进度 |
| `docs/acceptance/{offline,capacity,live-sdk}.md` | 分开记录离线、容量和真实联调证据 |
| `docs/runbooks/{deployment,recovery}.md` | 部署、观察与恢复操作 |

### Task 1: 真实模块协作的固定场景与全流程验收

**Files:**
- Create: `backend/tests/acceptance/conftest.py`, `scenario.py`, `test_batch_flow.py`, `frontend/tests/acceptance-batch.spec.ts`。
- Modify: `backend/tests/fakes/tiktok.py`、模块测试 fixture 注册。

**Interfaces:**
- Consumes: P05 的 `create_draft`、`prepare_draft`、`generate_preview`、`get_preview_summary`；P06 的 `submit_preview`、`get_submission`。
- Consumes: P06 `FakeTikTokAPI.call_api(resource_path,http_method,*args,**kwargs)`，返回实际 SDK 响应对象；`store` 保存远端对象，`calls` 保存脱敏调用。
- Produces: `AcceptanceScenario(context, bc_id, strategy_version_id, provider_connection_id, application_id, drama_lines, account_lines)`，以及 `pump_jobs(max_steps: int=1000) -> int` 测试驱动器。
- `pump_jobs` 只用于测试：投递 P01 outbox 后按已注册 Celery task 的原函数执行，继续处理新产生的任务直至无可运行步骤；超过 max_steps 失败并打印内部任务 ID，不跳过业务状态机。

- [ ] 使用单独验收数据库，构造下表数据。此处覆盖外层事务 fixture，让准备任务和新进程能看见已提交记录；禁止把“共享一个尚未提交的测试 session”作为跨进程恢复证据。

| 资源 | 固定场景 |
| --- | --- |
| 租户 T1/T2 | 独立管理员、投手和连接；平台管理员可进入两者 |
| T1 的 BC 与账户 | 1 BC、3 个 USD 可用账户；其中 2 个完整名称相同，测试歧义用完整 ID 消除 |
| T2 的账户 | 与 T1 不同远端账户；另造远端 ID 归属冲突，目录可见但业务拒绝 |
| 版权方 | 网眼和嘉书各一连接，使用独立 MockTransport/脱敏协议样例 |
| 剧目 | `The Bond`、`Hidden Promise`，各 23 份已接收素材；再加一份其他租户同名素材 |
| 来源 | 每部剧的素材分散在两个实际上传账户，目标素材映射由共享/原文件上传分别产生 |
| 策略 | 每组 10 份、N=2、Campaign 日预算 100 USD、目标 ROAS 1.2、固定版本文案池 |

- [ ] 实现 `scenario.py`：用 P02/P03/P04/P05 的模型和服务建立上述记录，内部 UUID 每次运行生成；提供上述接口所需的完整输入，禁止通过修改执行状态直接伪造成功结果。SDK 替身只代替外部边界，素材匹配、计划生成、权限、数据库和幂等均用真实实现。
- [ ] 写跨模块失败测试，再运行 `uv run pytest tests/acceptance/test_batch_flow.py -q`。核心断言如下，`ready_preview_id` fixture 必须通过真实 create/prepare/preview 流程生成，并由 `pump_jobs` 推进。

```python
from uuid import uuid4
from decimal import Decimal
from app.modules.builds.submissions import submit_preview, get_submission

def test_full_batch_creates_expected_enabled_objects(
    session, context, ready_preview_id, pump_jobs, fake_tiktok
):
    receipt = submit_preview(
        session, context=context, preview_id=ready_preview_id, request_id=uuid4()
    )
    session.commit()
    pump_jobs()
    session.expire_all()
    view = get_submission(session, context=context, submission_id=receipt.submission_id)
    assert view.status == "COMPLETED"
    assert view.succeeded.campaign_count == 6
    assert view.succeeded.adgroup_count == 18
    assert view.succeeded.ad_count == 36
    assert view.currency == "USD"
    assert Decimal(view.daily_budget_sum) == Decimal("600")
    creates = [call for call in fake_tiktok.calls if call["path"].endswith("/create/")]
    ad_creates = [call for call in creates if "/smart_plus/" in call["path"]]
    assert len(ad_creates) == 60
    assert all(call["body"]["operation_status"] == "ENABLE" for call in ad_creates)
```

- [ ] 在 `ready_preview_id` 中执行真实调用序列：`create_draft(... drama_lines=["The Bond","Hidden Promise"], account_lines=三个账户ID, link_config={})` → commit → `prepare_draft(... request_id=uuid4())` → commit/pump → `generate_preview(... expected_revision=1)` → commit/pump。检查预览 FROZEN 后返回 ID；不直接插入带 READY 的空预览。
- [ ] 分别用网眼和嘉书运行场景；增加币种不符排除一户、Minis 不可用只排除一组合、多剧命中素材无需确认三个变体，核对 submitted/excluded 数量与实际预算。
- [ ] Playwright 通过同一个测试后端执行批量输入与提交，断言无需剧目/账户下拉或再次勾选；编辑素材后旧预览不可提交；按钮明确实际创建数量；进度展示三层成功与异常数。运行 `bunx playwright test tests/acceptance-batch.spec.ts`、`bun run build`。
- [ ] 按[前端页面/状态矩阵](../specs/2026-09-08-tiktok-06-frontend-experience-design.md)记录 UI-01～UI-12 的实际覆盖。用 S01～S08 对照空白、加载、输入问题、部分阻断、旧预览/409、网络与未知、权限变化、完成/部分失败；在 Playwright fixtures 中构造，不能通过人工修改生产状态演示。
- [ ] UI-03→UI-11→UI-12 走完成功、币种不符部分提交、SP2明确失败续建、超时先回查四条流程；检查数量与预算、当前租户/BC、排除范围与冻结文案。UI-05验证精确命名提示及实际源账户只读；UI-07验证SP数量不倍增Campaign预算；平台切换不迁移草稿。
- [ ] 1440×900、1280×800 验证主操作/表格和底栏无遮挡，390px宽验证收起导航、表单单列、表格局部滚动；键盘可操作 Sheet、关闭返回焦点、状态有文字。海量数据验证归入Task3容量测试，不能用小样本原型宣称容量通过。
- [ ] 提交 `acceptance: verify tenant batch flow end to end`，在 `docs/acceptance/offline.md` 记录所用场景及命令。

### Task 2: 租户隔离与失败恢复的跨模块检查

**Files:**
- Create: `backend/tests/acceptance/test_isolation_recovery.py`。
- Modify: `backend/tests/acceptance/conftest.py`, `backend/tests/fakes/tiktok.py`, `docs/acceptance/offline.md`。

**Interfaces:**
- Consumes: P06 的稳定执行步骤键、UNKNOWN 核查、retry/reconcile 接口和 P01 outbox。
- Produces: 丢响应、重复投递、权限撤销和租约失效的测试证据；不增加生产“跳过核查”接口。

- [ ] 写“远端成功后丢失响应”的包装器：先调用 FakeTikTokAPI 保存真实模拟远端对象，再抛超时；后续 get 仍返回该对象。

```python
class DropFirstCreateReply:
    def __init__(self, backend):
        self.backend = backend
        self.dropped = False

    def __call__(self, resource_path, http_method, *args, **kwargs):
        result = self.backend.call_api(resource_path, http_method, *args, **kwargs)
        if resource_path.endswith("/smart_plus/ad/create/") and not self.dropped:
            self.dropped = True
            raise TimeoutError("simulated reply loss after remote commit")
        return result
```

- [ ] 执行第一轮任务断言步骤 UNKNOWN；通过核查得到 Ad ID，再续跑剩余步骤；同一目标广告只有一次 create 调用。若查询不能唯一确定匹配，状态保持 NEEDS_REVIEW，不能按超时长短推断不存在。
- [ ] 写双击提交和更换 HTTP 请求幂等键测试：同一预览仍只产生一个 Submission。重放相同 Celery 消息、重复 outbox 投递都复用成功步骤；对已经成功的 Campaign 不能再次 create。
- [ ] 覆盖权限撤销：广告 SP1 已成功后停用成员/连接，SP2 尚未创建则暂停本地剩余步骤；断言没有调用远端 pause/delete/status update，SP1 保留 ENABLE。
- [ ] 覆盖跨租户调用：T2 读取 T1 的链接、素材签名 URL、预览、任务 ID 返回不可见；T1 Worker 不得用 T2 连接补失败；平台代操作记录真实 actor_id 和目标 tenant_id。
- [ ] 覆盖独立连接和进程可见性：已提交步骤 RUNNING 的租约到期后，新 session 恢复时先核查；原 Worker 晚到的响应只能按持有的尝试标识更新，不能覆盖后来步骤状态。用两个独立 DB session 验证，不能仅 mock 状态枚举。
- [ ] 执行 `uv run pytest tests/acceptance/test_isolation_recovery.py -q`，预期上述失败注入均保持对象数、预算与冻结文案一致。提交 `acceptance: verify isolation and recovery under failures`。

### Task 3: 大量账户与批次展开的容量检查

**Files:**
- Create: `backend/tests/performance/test_large_batch.py`, `backend/scripts/benchmark_batches.py`, `docs/acceptance/capacity.md`。
- Modify: 发现瓶颈时对应模块索引或分页代码，仅针对证据调整。

**Interfaces:**
- Consumes: P02 账户目录/批量解析，P05 流式组合展开与分页预览，P06 租户公平调度、共享 API 准入。
- Produces: 每档目录数量、展开数量、响应分页、P95、峰值内存、任务恢复时间和验证环境；这些测试数据不是平台 QPS 限额或用户账户数量上限。

- [ ] 基准脚本使用固定输入参数：`--accounts`、`--dramas`、`--target-accounts`、`--group-size`、`--creative-count`、`--seed`、`--output`。只写专用测试数据库，SDK 边界接替身，禁止用生产账户跑容量压测。
- [ ] 依次验证 1,000 / 10,000 / 100,000 条账户目录。大批场景使用 200 部剧 × 1,000 个目标账户、每剧 30 素材、K=10、N=2：200,000 Campaign 单元、600,000 Group、1,200,000 Ad 计划记录；允许分批持久化，不能一次构造全部记录列表。
- [ ] 增加分页不丢不重、有限响应大小与预算独立于 SP 数量的回归；函数 `read_account_pages`、`read_preview_pages` 在脚本中循环消费各模块 `next_cursor`，返回指标而非全量收集业务对象。

```python
from time import perf_counter

def measure_pages(fetch_page):
    cursor = None
    rows = 0
    page_max = 0
    durations = []
    while True:
        started = perf_counter()
        page = fetch_page(cursor)
        durations.append(perf_counter() - started)
        rows += len(page.items)
        page_max = max(page_max, len(page.items))
        cursor = page.next_cursor
        if cursor is None:
            break
    durations.sort()
    position = max(0, (95 * len(durations) + 99) // 100 - 1)
    return {"rows": rows, "page_max": page_max, "p95_seconds": durations[position]}
```

- [ ] 对目标列表/预览分页大小设本次测试值 200，断言 `page_max <= 200`；这是传输页大小，不是账户数量上限。计数查询单独返回汇总，前端不为总数下载全部明细。
- [ ] 同时运行 T1 大任务与 T2 小任务，记录 T2 首个可执行单元等待时间；共享额度的多 Worker 合计不能超过测试配置。注入429时应延迟并释放 Worker，不通过 sleep 占住处理线程。
- [ ] 记录批次展开的进程 RSS 或容器峰值内存；将规模增加 10 倍时检查没有同等倍数的全量内存常驻。若数据库计划变慢，保留 EXPLAIN ANALYZE 的脱敏结果，按实际过滤/排序添加索引后只重跑受影响基准。
- [ ] 运行命令并保存实际结果。允许先跑小档验证脚本再运行大档；硬件、并发与限制均写在记录内，不提前填 P95 或吞吐达标数字。

```bash
uv run pytest tests/performance/test_large_batch.py -q
uv run python scripts/benchmark_batches.py --accounts 100000 --dramas 200 --target-accounts 1000 --group-size 10 --creative-count 2 --seed 20260908 --output ../docs/acceptance/capacity-run.json
```

- [ ] 提交 `acceptance: benchmark large account directories and batches`。脱敏指标可提交；百万条合成记录和完整快照不进入 Git。

### Task 4: 真实授权与 SDK 能力逐项联调

**Files:**
- Create: `docs/acceptance/live-sdk.md`, `backend/tests/contracts/fixtures/tiktok/README.md`。
- Modify: P02 权限映射、P03 版权方归因契约、P06 场景能力契约对应的 fixture 和回归测试。

**Interfaces:**
- Consumes: 实际部署地址、获批 App、租户授权连接、测试素材与版权方配置。
- Produces: 真实读写操作证据、SDK/字段/权限兼容结论；缺少输入时对应项目保持“未执行”，不填写模拟成功。

- [ ] 使用 P01 交付的真实 HTTPS 回调申请/配置 App；授权返回同一租户连接。验证 state 过期、重复回调、重新授权失败不破坏旧连接。
- [ ] 通过该租户官方 SDK 调用获得 BC 及全部授权账户，保留分页参数、request ID 和脱敏返回结构；核实权限字段映射后再将 P02 的 `UNKNOWN` 转为对应业务能力，不能把目录可见等同于可上传/可创建。
- [ ] 核对 P06 Minis/Identity/CTA 能力：专用 SDK 方法有覆盖时直接调用；缺少 Minis 专用方法时，仅使用官方 ApiClient 的通用入口调用经官方文档核实的端点和参数。记录 SDK revision、官方字段依据和真实返回；不增加手写 HTTP 传输。
- [ ] 分别验证网眼、嘉书的剧目解析、相同配置链接复用、配置冲突不覆盖、归因基础名来源。对网眼旧 CLI 中客户端构造的名称，必须保留已核实的版权方/前端契约样例，不能凭旧注释当作服务端原始字段。
- [ ] 上传真实测试素材，确认系统选定账户、实际 VID/MID 和封面；向另一个已授权账户分发并回查目标标识。原文件重新上传路径如被采用，独立记录该路径及账户权限，不把它记成素材共享。
- [ ] 形成场景兼容表：SDK方法/通用入口、必要字段、权限证据、账户币种、素材限制、CTA有效候选、结果状态；未知项保持不支持或不可提交，不能静默改变用户策略。
- [ ] 执行一笔用户在工具中审阅并提交的具体试投。页面必须展示租户、BC、账户、剧目、素材、预算、ROAS 和实际数量；一次“创建并启用”提交后，三级请求直接 ENABLE。没有第二次启用确认。
- [ ] 读取实际三层对象验证参数及操作状态；审核中仍可标记创建完成，但不声称审核通过或发生消耗。保存脱敏 request ID 与对象 ID 的受控记录，不把真实投放数据公开提交到仓库。
- [ ] 将发现的字段差异补成最小离线回归，重跑对应模块测试；完成证据缺项前不将真实搭建标记为已联调。

### Task 5: 部署和恢复手册可执行

**Files:**
- Create: `docs/runbooks/deployment.md`, `docs/runbooks/recovery.md`。
- Modify: `compose.staging.yml`, 生产配置及 `docs/acceptance/offline.md`。

**Interfaces:**
- Consumes: 当前通过验收的镜像、单一迁移 head、数据库/对象存储持久卷及密钥配置。
- Produces: 登录、连接、队列状态、恢复步骤和发布版本的实际核查记录。

- [ ] 发布前核对：`alembic heads` 单一 head，新库和上一阶段数据库升级均通过；镜像固定 tag/digest，API 与 Worker 同版本；密钥只来自运行环境，使用原凭据加密密钥才能恢复连接。
- [ ] 运行 `docker compose -f compose.yml -f compose.staging.yml config --quiet`，迁移成功后再启动 API/Worker/Beat；验证登录、回调和前端 `/api` 路径。不要在日志或文档粘贴展开后的密钥。
- [ ] 手册分清三种恢复入口：确定失败用 retry；未知写结果或参数差异用 reconcile；修改投放内容生成新预览/批次。不得通过清空 ExecutionStep、删除成功对象或强制退回 PENDING 来消除错误。
- [ ] 依次演练 Redis 短暂不可用、Worker 重启、连接失权：outbox 保留未投递任务；步骤按租约核查；只停止尚未执行的步骤，已启用广告不自动停投。
- [ ] 用测试备份恢复到独立库，保留素材对象和加密密钥。恢复后的未确认写入统一先核查远端，避免数据库较旧而重建已存在广告；恢复演练不连接生产 Worker 自动开跑。
- [ ] 手册记录查看入口：队列积压、单步骤失败原因、UNKNOWN 数、最后权限核验时间、每租户等待时间；不新增经营指标同步工程。
- [ ] 提交 `release: document deployment and recovery evidence`。最终记录分别写明离线流程、容量测试、真实 SDK、真实试投与实际部署五项状态。

## 本阶段完成条件

离线集成、故障恢复、容量和页面测试均有真实运行证据；真实 SDK 与试投状态单独可追踪。若开发者 App 或部署配置尚未具备，允许交付已测试的本地工程与部署包，但明确标注尚未执行的外部验收，不能宣称完整投放链路已经可用。
