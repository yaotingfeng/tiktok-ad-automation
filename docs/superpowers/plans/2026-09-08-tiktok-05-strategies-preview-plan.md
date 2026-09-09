# TikTok 投放策略与搭建预览 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现租户策略版本、100 条英文文案、批量输入、全账户素材分组和可编辑后冻结的搭建预览。

**Architecture:** 策略模块保存版本，搭建模块按剧目和账户逐行生成计划，预览只读取资源状态，不创建 TikTok 广告或分发素材。SQLModel/Alembic 保存草稿、预览、素材组、广告变体和排除项；前端以 shadcn 表单及分页明细展示同一个冻结版本，提交执行交给计划 06。

**Tech Stack:** 固定官方 FastAPI 全栈模板、Python >=3.14、SQLModel、Alembic、PostgreSQL、Pydantic、Celery/outbox、React、TypeScript、shadcn、Bun、Playwright。

**Spec:** [04 投放策略](../specs/2026-09-08-tiktok-04-strategies-design.md)、[05 搭建执行](../specs/2026-09-08-tiktok-05-build-execution-design.md)。执行前同时阅读这两份已批准设计。

## Global Constraints

- 应用根目录为 `/Users/yaotingfeng/Documents/ytf/ytf-os-ad-skill/tiktok-ad-automation`，下文代码路径均相对该目录。目录已在 2026-09-09 迁入当前工作区，实施状态见 `docs/implementation-progress.md`。
- 依赖计划 01～04；模板与 SDK revision、锁文件、共享身份/错误/分页/outbox 沿用计划 01，不复制基础设施。
- “Campaign、Ad Group、Ad 的创建请求均直接使用 `operation_status=ENABLE`。”本计划不发起这些创建。
- “单次提交只能包含一个租户、一个 BC，以及该上下文授权范围内的目标账户。”
- “版权方与投放策略可以选择；剧目名称和目标广告账户均通过批量粘贴输入。”解析后不要求再次勾选。
- 每剧覆盖所有输入有效账户；按文件名升序、素材 ID 稳定并列，每组一个 Ad Group，保留尾组，每组创建 N 条普通 Smart+ Ad。
- 文案池为 100 条英文通用文案；同组无放回抽样，本批同一剧目素材组跨账户复用结果，预览保存具体文案，执行和重试不重新随机。
- 仅完整剧名字面包含匹配，忽略大小写与剧名首尾空白；同素材命中多剧只提示，不阻断或二次确认，不永久归剧。
- Campaign 日预算共享给旗下 Ad Group，策略明确币种与目标 ROAS；不做隐式换汇。
- 受保护归因基础名原样保留，仅后缀模板可编辑；日期、批次短码、组号和 SP 号在预览固定。
- READY 与存在合法分发路径的 PREPARING 可提交，BLOCKED 明示排除；修复后的补建只包含原先未提交组合。
- API 前缀为 `/api/tenants/{tenant_id}/`；不存在 `/api/v1`，租户来自鉴权上下文。
- 不包含经营数据同步、分析库、投后调价、额外激活步骤或修改旧广告状态。
- 后端命令在 `backend/` 执行，前端命令在 `frontend/` 执行；实际实施时每个 Task 通过检查后提交一次，当前不执行 Git 写操作。

---

## Files 与跨计划契约

| 文件 | 唯一职责 |
|---|---|
| `backend/app/modules/strategies/{models,schemas,service,api}.py` | 版本实体、类型校验、事务服务、租户 API |
| `backend/app/modules/strategies/{copy_pool,grouping,naming}.py` | 固定英文内容、纯函数分组/抽样、受保护名称 |
| `backend/app/modules/builds/{models,schemas,drafts,previews,api}.py` | 草稿、分页预览、冻结与 API；执行文件由计划 06 添加 |
| `backend/app/modules/builds/preview_tasks.py` | 通过 Celery 分批生成预览，保存进度 |
| `backend/app/alembic/versions/0005_strategies_build_previews.py` | 表、唯一约束、复合外键与冻结约束 |
| `backend/tests/modules/strategies/`、`backend/tests/modules/builds/` | 纯函数、事务、分页和鉴权回归 |
| `frontend/src/features/strategies/`、`frontend/src/features/builds/` | 表单、粘贴输入、素材组编辑、预览 |
| `frontend/tests/strategies.spec.ts`、`frontend/tests/build-preview.spec.ts` | 用户输入与预览冻结的浏览器回归 |

共享输入：`TenantContext(tenant_id: UUID, actor_id: UUID, role: str)`、`DomainError(code, message, retryable=False)`、`Page[T](items, next_cursor)`，不在模块内重新定义。

| 输入接口 | 精确契约 |
|---|---|
| `require_tenant` | `(session, *, actor_id: UUID, tenant_id: UUID, action: str) -> TenantContext`；读页面用 `read`，策略写用 `strategy_write`，搭建用 `build` |
| `resolve_account_access` | `(session, *, context, bc_id: str, advertiser_id: str, action: str) -> AccountAccess`，包含 `connection_id/currency/timezone` |
| `prepare_links` | `providers.service(session, *, context, connection_id: UUID, application_id: str, lines: list[str], config: dict, request_id: UUID) -> UUID` |
| `get_link_results` | `providers.service(session, *, context, task_id: UUID, cursor: str | None = None) -> Page[ResolvedLink]` |
| `match_materials` | `materials.service(session, *, context, bc_id: str, title: str, cursor: str | None = None) -> Page[MaterialCandidate]` |
| `get_material_readiness` | `materials.service(session, *, context, bc_id: str, material_id: UUID, advertiser_id: str) -> MaterialReadiness`；纯读取，不上传 |
| `enqueue_after_commit` | `app.jobs.outbox(session, *, context, task_name, task_key, payload) -> UUID`，与当前业务事务共同提交 |

实施顺序包含一个显式依赖：本计划 Task 1～4 → 计划 06 Task 2 的场景读取 → 本计划 Task 5～6；计划 06 Task 2 不读取执行表，避免循环。

`ResolvedLink` 使用 `drama_id/external_drama_id/title/link_id/url/protected_base/tiktok_minis_id/status`；ready 时除 `tiktok_minis_id` 外前述事实齐全，基础名允许契约明确的空串。`application_id` 不是 Minis ID。
`MaterialCandidate` 使用 `material_id/file_name/bc_id/original_available/source_assets`；`MaterialReadiness.state` 为 `ready/preparable/blocked`，`path` 为 `existing_target/share_source/upload_original/unavailable`。预览不调用 `ensure_target_asset`。
共享测试夹具为 `session/context/client/redis_client`；本模块 `conftest.py` 通过实际测试租户记录构造 `other_context`，不复用真实账户数据。

### Task 1: 策略版本与金额类型

**Files:**
- Create: `backend/app/modules/strategies/models.py`、`schemas.py`、`service.py`。
- Create: `backend/app/alembic/versions/0005_strategies_build_previews.py`；本计划后续 Task 在同一尚未交付的迁移中加入预览表。
- Test: `backend/tests/modules/strategies/test_versions.py`。

**Interfaces:**
- Consumes: `TenantContext`、`DomainError`、`require_tenant(..., action="strategy_write")`。
- Produces: `StrategyConfig`、`create_strategy(session, *, context, name: str, config: StrategyConfig) -> UUID`、`append_version(session, *, context, strategy_id: UUID, config: StrategyConfig) -> UUID`、`get_version(session, *, context, version_id: UUID) -> StrategyConfig`。

- [x] **Step 1: 添加金额及不可变版本的失败测试。** 在 `test_versions.py` 导入下面实际接口；copy_pool_version使用迁移创建、Task 2填充的固定池版本UUID。

```python
from decimal import Decimal
from uuid import UUID
import pytest
from app.core.errors import DomainError
from app.modules.strategies.schemas import StrategyConfig
from app.modules.strategies.service import create_strategy, append_version, get_version
from app.modules.strategies.models import StrategyVersion
from sqlmodel import select

def test_version_keeps_budget_and_tenant(session, context, other_context):
    config = StrategyConfig(budget="100.25", currency="USD", target_roas="1.08",
        group_size=10, creative_count=2,
        copy_pool_version=UUID("02a4e656-a330-40dc-864c-26e81961f3ca"))
    strategy_id = create_strategy(session, context=context, name="普通短剧", config=config)
    original_id = session.exec(select(StrategyVersion.id).where(
        StrategyVersion.tenant_id == context.tenant_id,
        StrategyVersion.strategy_id == strategy_id)).one()
    version_id = append_version(session, context=context, strategy_id=strategy_id,
        config=config.model_copy(update={"budget": Decimal("200.00")}))
    session.flush()
    assert get_version(session, context=context, version_id=version_id).budget == Decimal("200.00")
    assert get_version(session, context=context, version_id=original_id).budget == Decimal("100.25")
    with pytest.raises(DomainError, match="strategy_not_found"):
        get_version(session, context=other_context, version_id=version_id)
```

- [x] **Step 2: 运行红测。** `uv run pytest tests/modules/strategies/test_versions.py -q`；预期新模块尚不存在导致失败。
- [x] **Step 3: 添加严格配置类型。** `extra="forbid"` 拒绝旧账户池、窗口和预算复制字段；Decimal 存入 NUMERIC，不经过浮点计算预算。

```python
from decimal import Decimal
from uuid import UUID
from pydantic import BaseModel, ConfigDict, Field

class StrategyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    budget: Decimal = Field(gt=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    target_roas: Decimal = Field(gt=0)
    group_size: int = Field(gt=0)
    creative_count: int = Field(gt=0)
    copy_pool_version: UUID
    cta_option_ids: tuple[str, ...] = ()
    campaign_suffix: str = "-{YYYYMMDD}-{batch_short_id}"
```

- [x] **Step 4: 实现事务版本服务和迁移。** `Strategy(id, tenant_id, name, active, latest_version)`；`StrategyVersion(id, tenant_id, strategy_id, number, copy_pool_version_id, config JSONB, created_by, created_at)`。对 `(tenant_id,strategy_id,number)` 唯一，版本外键包含租户。先创建空的固定 CopyPoolVersion 记录，正文由 Task 2 填充；策略版本独立外键引用该池，不能只把引用藏在 JSON 内。以下 `append_version` 是核心事务，不在服务内部提交：

```python
from uuid import uuid4
from sqlmodel import select
from app.core.errors import DomainError
from app.modules.strategies.models import Strategy, StrategyVersion
from app.modules.strategies.schemas import StrategyConfig

def append_version(session, *, context, strategy_id, config):
    config = StrategyConfig.model_validate(config.model_dump())
    strategy = session.exec(select(Strategy).where(
        Strategy.id == strategy_id, Strategy.tenant_id == context.tenant_id
    ).with_for_update()).one_or_none()
    if strategy is None or not strategy.active:
        raise DomainError("strategy_not_found", "strategy_not_found")
    strategy.latest_version += 1
    row = StrategyVersion(id=uuid4(), tenant_id=context.tenant_id,
        strategy_id=strategy.id, number=strategy.latest_version,
        copy_pool_version_id=config.copy_pool_version,
        config=config.model_dump(mode="json"), created_by=context.actor_id)
    session.add(strategy)
    session.add(row)
    session.flush()
    return row.id
```

`create_strategy` 插入 `latest_version=0` 主记录后调用此函数；`get_version` 用租户+版本 ID 查询并 `StrategyConfig.model_validate(row.config)`，未找到统一 `strategy_not_found`。迁移为策略版本表加拒绝 UPDATE/DELETE 的触发器，停用仅更新主表。为两并发版本追加增加唯一性测试，为跨租户版本引用增加外键失败测试。
- [x] **Step 5: 运行迁移与绿测。** `uv run alembic upgrade head`；`uv run pytest tests/modules/strategies/test_versions.py -q`，预期全部通过，旧版本保持原金额。
- [x] **Step 6: 提交本 Task。** `git add backend/app/modules/strategies backend/app/alembic/versions/0005_strategies_build_previews.py backend/tests/modules/strategies`；`git commit -m "strategies: persist tenant strategy versions"`。

### Task 2: 100 条英文文案、素材分组和确定性 SP 抽样

**Files:**
- Create: `backend/app/modules/strategies/copy_pool.py`、`grouping.py`。
- Modify: `backend/app/modules/strategies/models.py`、`schemas.py`、迁移 `0005_strategies_build_previews.py`。
- Test: `backend/tests/modules/strategies/test_grouping.py`。

**Interfaces:**
- Consumes: `StrategyConfig`、`MaterialCandidate`。
- Produces: `CopyChoice(copy_id: UUID, text: str)`、`GroupPlan(group_no: int, material_ids: tuple[UUID, ...], copies: tuple[CopyChoice, ...])`、`seed_copies() -> tuple[CopyChoice, ...]`、`make_groups(materials, *, group_size: int, creative_count: int, pool: tuple[CopyChoice, ...], seed: int) -> tuple[GroupPlan, ...]`。

- [x] **Step 1: 添加失败测试，验证真正的尾组、文案唯一及重复执行。** 使用 `SimpleNamespace` 只模拟本函数读取的素材字段，DB/账户测试不使用该替身。

```python
from types import SimpleNamespace
from uuid import UUID
import pytest
from app.core.errors import DomainError
from app.modules.strategies.copy_pool import seed_copies
from app.modules.strategies.grouping import make_groups

def test_tail_and_frozen_copy_choices():
    pool = seed_copies()
    assert len(pool) == len({x.text for x in pool}) == 100
    files = [SimpleNamespace(material_id=UUID(int=i+1), file_name=f"Drama-{i:02}.mp4")
             for i in reversed(range(23))]
    first = make_groups(files, group_size=10, creative_count=3, pool=pool, seed=51)
    assert [len(x.material_ids) for x in first] == [10, 10, 3]
    assert all(len({c.text for c in x.copies}) == 3 for x in first)
    assert first == make_groups(files, group_size=10, creative_count=3, pool=pool, seed=51)
    with pytest.raises(DomainError, match="copy_pool_exhausted"):
        make_groups(files, group_size=10, creative_count=101, pool=pool, seed=51)
```

- [x] **Step 2: 运行红测。** `uv run pytest tests/modules/strategies/test_grouping.py -q`，预期缺少文案/分组实现。
- [x] **Step 3: 用以下可审阅的 10×10 固定组合生成全部 100 条种子。** 按双循环顺序落库，保存实际正文；运行期间不调用 AI，不把这些正文作为 CTA。

```python
from dataclasses import dataclass
from uuid import UUID, uuid5

POOL_VERSION = UUID("02a4e656-a330-40dc-864c-26e81961f3ca")
OPENERS = (
    "A story for your next break.", "Your next short drama awaits.",
    "Make room for a little drama.", "Take a moment for a new story.",
    "Enjoy a story one episode at a time.", "Discover a short drama today.",
    "Add a new story to your day.", "Spend a little time with a new drama.",
    "There is a story ready to explore.", "Start your next drama break.",
)
ENDINGS = (
    "Watch an episode.", "Start watching today.", "See where the story goes.",
    "Discover what happens next.", "Take a look at the story.",
    "Follow the story from here.", "Find a moment to watch.",
    "Explore the next scene.", "Let the story unfold.", "Begin with one episode.",
)

@dataclass(frozen=True)
class CopyChoice:
    copy_id: UUID
    text: str

def seed_copies():
    texts = tuple(f"{start} {end}" for start in OPENERS for end in ENDINGS)
    assert len(texts) == len(set(texts)) == 100
    return tuple(CopyChoice(uuid5(POOL_VERSION, text), text) for text in texts)
```

- [x] **Step 4: 编写完整纯函数分组。** 每个剧目素材组生成一次，后续账户行引用同一 GroupPlan；不得在账户循环里调用抽样。有效池正文去重后再判断容量。

```python
from dataclasses import dataclass
from random import Random
from uuid import UUID
from app.core.errors import DomainError
from app.modules.strategies.copy_pool import CopyChoice

@dataclass(frozen=True)
class GroupPlan:
    group_no: int
    material_ids: tuple[UUID, ...]
    copies: tuple[CopyChoice, ...]

def make_groups(materials, *, group_size, creative_count, pool, seed):
    if group_size < 1 or creative_count < 1:
        raise DomainError("invalid_group_config", "invalid_group_config")
    unique = {item.material_id: item for item in materials}
    ordered = sorted(unique.values(), key=lambda x: (x.file_name, str(x.material_id)))
    copies = tuple({item.text: item for item in pool}.values())
    if creative_count > len(copies):
        raise DomainError("copy_pool_exhausted", "copy_pool_exhausted")
    rng = Random(seed)
    return tuple(GroupPlan(index // group_size + 1,
        tuple(x.material_id for x in ordered[index:index + group_size]),
        tuple(rng.sample(copies, creative_count)))
        for index in range(0, len(ordered), group_size))
```

扩展 `CopyPoolVersion` 并增加 `CopyEntry` 表和不可变版本约束，首次安装幂等 seed，列表以 `copy_id` 唯一。编辑内容产生新池版本；禁用条目不改旧版本。对空素材返回零组、同名不同 ID 不误合并、手动删除后尾组、98 条有效正文配 N=99 补充参数化测试。官方文案长度限制由计划 06 的场景能力校验，不在此臆定上限。
- [x] **Step 5: 绿测并提交。** `uv run pytest tests/modules/strategies/test_grouping.py -q`，预期全部通过；`git add backend/app/modules/strategies backend/tests/modules/strategies backend/app/alembic/versions/0005_strategies_build_previews.py`；`git commit -m "strategies: freeze grouped creative copy choices"`。

### Task 3: 受保护名称和策略校验 API

**Files:**
- Create: `backend/app/modules/strategies/naming.py`、`api.py`。
- Modify: `backend/app/api/main.py`、`backend/app/modules/strategies/service.py`。
- Test: `backend/tests/modules/strategies/test_naming.py`、`test_api.py`。

**Interfaces:**
- Consumes: `StrategyConfig`、`require_tenant`、Task 1 版本服务。
- Produces: `render_names(*, protected_base: str, title: str, date_text: str, batch_short_id: str, suffix: str, group_no: int, creative_no: int, max_length: int) -> tuple[str, str, str]`；策略及文案池 API，路径与设计一致。

- [x] **Step 1: 添加前缀完整性与模板注入失败测试。**

```python
import pytest
from app.core.errors import DomainError
from app.modules.strategies.naming import render_names

def test_protected_prefix_survives():
    args = dict(protected_base="{b30008/s328302/c3}-The Bond", title="The Bond",
        date_text="20260908", batch_short_id="B7K2M9Q4", group_no=1, creative_no=2,
        max_length=200)
    names = render_names(**args, suffix="-{YYYYMMDD}-{batch_short_id}")
    assert names[2] == "{b30008/s328302/c3}-The Bond-20260908-B7K2M9Q4-g01-sp2"
    with pytest.raises(DomainError, match="invalid_name_template"):
        render_names(**args, suffix="-{protected_base.__class__}")
    with pytest.raises(DomainError, match="name_too_long"):
        render_names(**{**args, "max_length": 15}, suffix="-{YYYYMMDD}-{batch_short_id}")
```

- [x] **Step 2: 运行红测。** `uv run pytest tests/modules/strategies/test_naming.py -q`，预期命名函数缺失。
- [x] **Step 3: 添加白名单后缀实现。** `max_length` 来自已验证场景约束，测试值仅是 fixture，不是平台上限。

```python
from string import Formatter
from app.core.errors import DomainError

def render_names(*, protected_base, title, date_text, batch_short_id, suffix,
                 group_no, creative_no, max_length):
    values = {"YYYYMMDD": date_text, "batch_short_id": batch_short_id}
    try:
        parts = tuple(Formatter().parse(suffix))
    except ValueError as exc:
        raise DomainError("invalid_name_template", "invalid_name_template") from exc
    fields = {field for _, field, _, _ in parts if field is not None}
    if fields - values.keys() or "batch_short_id" not in fields:
        raise DomainError("invalid_name_template", "invalid_name_template")
    if any(spec or conversion for _, _, spec, conversion in parts):
        raise DomainError("invalid_name_template", "invalid_name_template")
    base = protected_base if protected_base else title
    campaign = base + suffix.format_map(values)
    group = f"{campaign}-g{group_no:02d}"
    ad = f"{group}-sp{creative_no}"
    if not base or any(len(name) > max_length for name in (campaign, group, ad)):
        raise DomainError("name_too_long", "name_too_long")
    return campaign, group, ad
```

- [x] **Step 4: 注册 API 并固定鉴权动作。** 所有路由先 `require_tenant`；GET 使用 read，POST 创建/版本使用 strategy_write。返回 config 的 Decimal 为十进制字符串；支持策略停用、创建版本、获取指定池版本和只读 validate。API 的 validate 聚合字段错误、池容量和模板错误，不访问 TikTok。代码关键点：

```python
from string import Formatter

def validate_strategy(config, pool):
    errors = []
    if config.creative_count > len({item.text for item in pool}):
        errors.append({"field": "creative_count", "code": "copy_pool_exhausted"})
    fields = {f for _, f, _, _ in Formatter().parse(config.campaign_suffix) if f}
    if fields - {"YYYYMMDD", "batch_short_id"} or "batch_short_id" not in fields:
        errors.append({"field": "campaign_suffix", "code": "invalid_name_template"})
    return errors
```

该函数与 `render_names` 共用模板解析函数，实际提交时也调用完整检查；API 将模板 parse 的 ValueError 转为 `invalid_name_template`。`test_api.py` 验证跨租户读取统一 404、普通无策略权限成员写入 403、未知字段 422、停用不会调用任何 SDK status 方法。
- [x] **Step 5: 绿测并提交。** `uv run pytest tests/modules/strategies -q`；预期全部通过。`git add backend/app/modules/strategies backend/app/api/main.py backend/tests/modules/strategies`；`git commit -m "strategies: validate protected names and tenant APIs"`。

### Task 4: 草稿输入、资源准备与可追溯编辑

**Files:**
- Create: `backend/app/modules/builds/models.py`、`schemas.py`、`drafts.py`。
- Modify: 迁移 `0005_strategies_build_previews.py`。
- Test: `backend/tests/modules/builds/test_drafts.py`、`conftest.py`。

**Interfaces:**
- Consumes: `prepare_links/get_link_results/match_materials/resolve_account_access`。
- Produces: `create_draft(session, *, context, bc_id: str, strategy_version_id: UUID, provider_connection_id: UUID, application_id: str, drama_lines: list[str], account_lines: list[str], link_config: dict) -> UUID`；`prepare_draft(session, *, context, draft_id: UUID, request_id: UUID) -> UUID`；`edit_material_groups(session, *, context, draft_id: UUID, drama_id: UUID, expected_revision: int, groups: list[list[UUID]]) -> int`。

- [x] **Step 1: 添加多页素材与多剧命中失败测试。** `test_drafts.py` 的 mock 只替换资源服务，不替换搭建逻辑。

```python
from types import SimpleNamespace
from uuid import UUID
from app.core.pagination import Page
from app.modules.builds.drafts import collect_pages

def test_all_pages_and_shared_material_are_preserved():
    first = SimpleNamespace(material_id=UUID(int=1), file_name="Long Drama - Short Drama.mp4")
    second = SimpleNamespace(material_id=UUID(int=2), file_name="Short Drama - 02.mp4")
    pages = {None: Page(items=[first], next_cursor="page2"),
             "page2": Page(items=[second], next_cursor=None)}
    assert [x.material_id for x in collect_pages(lambda cursor: pages[cursor])] == [UUID(int=1), UUID(int=2)]
    assert list(collect_pages(lambda cursor: pages[cursor]))[0].material_id == first.material_id
```

- [x] **Step 2: 红测。** `uv run pytest tests/modules/builds/test_drafts.py -q`，预期缺少草稿函数。
- [x] **Step 3: 添加完整分页读取器及行实体。** 每页处理后入库，不把账号×剧目在此展开。循环游标异常明确报错，避免无限请求。

```python
from app.core.errors import DomainError

def collect_pages(fetch):
    cursor = None
    seen = set()
    while True:
        page = fetch(cursor)
        yield from page.items
        cursor = page.next_cursor
        if cursor is None:
            return
        if cursor in seen:
            raise DomainError("repeated_cursor", "repeated_cursor")
        seen.add(cursor)
```

表为 `BuildDraft(id,tenant_id,bc_id,revision,strategy_version_id,provider_connection_id,application_id,link_config)`、`DraftInput(id,draft_id,tenant_id,kind,line_no,raw_text,status,reason)`、`DraftDrama(draft_id,tenant_id,drama_id,link_id,title)`、`DraftAccount(draft_id,tenant_id,advertiser_id,currency,connection_id)`、`DraftGroupMaterial(draft_id,tenant_id,drama_id,group_no,position,material_id)`。所有关系使用租户复合外键；材料 FK 指向计划 04 内部 ID。
- [x] **Step 4: 实现 prepare/edit 的具体事务规则。** `prepare_draft` 按 `request_id` 幂等调用版权方准备，保存任务 ID，再分页消费结果；账户输入交给计划 02 精确 ID/全名解析，输入行全部保留反馈，实际账户按 ID 去重。剧目按连接、应用、外部剧 ID、链接配置去重；素材对每剧独立分页匹配，多剧命中只存提示。手动补选也验证租户/BC可见性。编辑用乐观锁：

```python
from sqlalchemy import update
from app.core.errors import DomainError
from app.modules.builds.models import BuildDraft

def bump_revision(session, *, context, draft_id, expected_revision):
    result = session.execute(update(BuildDraft).where(
        BuildDraft.id == draft_id, BuildDraft.tenant_id == context.tenant_id,
        BuildDraft.revision == expected_revision
    ).values(revision=expected_revision + 1))
    if result.rowcount != 1:
        raise DomainError("draft_revision_conflict", "draft_revision_conflict")
    return expected_revision + 1
```

每次修改材料、顺序、策略、账户、剧目或重抽文案都调用此函数；同一事务替换该草稿的分组关系并将旧预览标为过期，不修改素材主记录。增补事务测试：两编辑并发只有一个成功、自动匹配不需要勾选、多剧共用合法、不完整标题不做别名翻译、版权方 result_unknown 保留原行不变为无匹配。
- [x] **Step 5: 绿测并提交。** `uv run pytest tests/modules/builds/test_drafts.py -q`；预期分页完整、并发冲突可解释。`git add backend/app/modules/builds backend/tests/modules/builds backend/app/alembic/versions/0005_strategies_build_previews.py`；`git commit -m "builds: prepare editable drafts from pasted inputs"`。

### Task 5: 分批全笛卡尔积、只读就绪判断与冻结预览

**Files:**
- Create: `backend/app/modules/builds/previews.py`、`preview_tasks.py`。
- Modify: `models.py`、`schemas.py`、迁移 `0005_strategies_build_previews.py`。
- Test: `backend/tests/modules/builds/test_previews.py`。

**Interfaces:**
- Consumes: Task 1～4；`get_material_readiness`；场景检查由计划 06 的 `read_scene_context(session, *, context, bc_id: str, advertiser_id: str, link_id: UUID) -> SceneContext` 提供只读结果，离线测试直接提供脱敏 fixture。
- Produces: `generate_preview(session, *, context, draft_id: UUID, expected_revision: int) -> UUID`；`continue_preview(session, *, context, preview_id: UUID) -> bool` 每次推进一批并返回是否完成；`get_preview_summary(session, *, context, preview_id: UUID) -> PreviewSummary`；`get_preview_units(session, *, context, preview_id: UUID, cursor: str | None = None) -> Page[PreviewUnit]`；`load_frozen_unit(session, *, context, unit_id: UUID) -> FrozenUnit`（供计划 06）。
- `FrozenUnit`：`unit_id/preview_id/tenant_id/drama_id/link_id/strategy_version_id` 为 UUID，`bc_id/advertiser_id/connection_id/currency` 对应已授权账户（connection_id 为 UUID），`campaign_name/protected_base/url` 为 str，`budget/target_roas` 为 Decimal，`readiness` 为 READY/PREPARING/BLOCKED，`reason_codes` 为 tuple[str,...]，`scene_snapshot` 为已验证小型场景事实；组和 Ad 通过 `get_frozen_groups(session, *, context, unit_id, cursor=None) -> Page[FrozenGroup]` 分页。
- `FrozenGroup(group_id: UUID, group_no: int, name: str, material_ids: tuple[UUID,...], ads: tuple[FrozenAd,...])`；`FrozenAd(ad_id: UUID, creative_no: int, name: str, copy_id: UUID, text: str, cta_option_ids: tuple[str,...])`。一页只读取有限组，单组素材与 N 受场景校验。
- `PreviewUnit` 是分页展示DTO：`unit_id/drama_id: UUID, title/advertiser_id/campaign_name: str, readiness: Literal[READY,PREPARING,BLOCKED], reason_codes: list[str], group_count/ad_count: int`；详情通过冻结组分页接口加载，不把所有创意嵌入列表页。

- [x] **Step 1: 写失败测试，覆盖展开规模与预算。**

```python
from decimal import Decimal
from itertools import islice
from app.modules.builds.previews import iter_pairs, unit_readiness

def test_product_is_lazy_and_not_a_rotating_window():
    pairs = iter_pairs(range(100_000), lambda: iter(("a", "b", "c")))
    assert list(islice(pairs, 7)) == [(0,"a"),(0,"b"),(0,"c"),(1,"a"),(1,"b"),(1,"c"),(2,"a")]
    assert len(list(iter_pairs(range(2), lambda: iter(("a","b","c"))))) == 6
    assert unit_readiness("USD", "USD", ["ready", "preparable"], []) == "PREPARING"
    assert unit_readiness("USD", "EUR", ["ready"], []) == "BLOCKED"

def test_budget_is_derived_from_actual_campaigns(session, context, prepared_draft):
    from app.modules.builds.previews import generate_preview, continue_preview, get_preview_summary
    preview_id = generate_preview(session, context=context,
        draft_id=prepared_draft.draft_id, expected_revision=1)
    assert continue_preview(session, context=context, preview_id=preview_id)
    summary = get_preview_summary(session, context=context, preview_id=preview_id)
    assert summary.campaign_count == 6
    assert summary.adgroup_count == 18
    assert summary.ad_count == 36
    assert Decimal(summary.daily_budget_sum) == Decimal("600")
```

本测试的prepared_draft fixture在builds/conftest.py调用Task 4真实草稿服务建立2剧×3个USD账户，每剧23份素材，策略group_size=10/creative_count=2/budget=100；资源服务使用计划03/04的脱敏fixture，场景读服务返回计划06Task2已定义的有效SceneContext。少于200单元，一次continue_preview足够完成；不以手填summary代替系统结果。

- [x] **Step 2: 红测。** `uv run pytest tests/modules/builds/test_previews.py -q`，预期缺少展开与冻结逻辑。
- [x] **Step 3: 实现纯展开及就绪函数。**

```python
def iter_pairs(drama_ids, account_iterator_factory):
    for drama_id in drama_ids:
        for advertiser_id in account_iterator_factory():
            yield drama_id, advertiser_id

def unit_readiness(strategy_currency, account_currency, material_states, reasons):
    if strategy_currency != account_currency or reasons or not material_states:
        return "BLOCKED"
    if "blocked" in material_states:
        return "BLOCKED"
    return "PREPARING" if "preparable" in material_states else "READY"
```

- [x] **Step 4: 按以下完整步骤实现冻结事务。** 新增 `BuildPreview(id,tenant_id,bc_id,draft_id,draft_revision,batch_short_id,local_date,status,counts,content_digest)`、`BuildUnit`、`PreviewDramaGroup`、`PreviewGroupMaterial`、`PlannedGroup`、`PlannedAd`。每条外键含租户，`(preview_id,advertiser_id,drama_id)` 唯一，批次短码唯一，`(draft_id,draft_revision)` 对有效预览唯一。头先 BUILDING；每剧仅抽样一次写 `PreviewDramaGroup`，再按账户键集分页展开；每 200 单元提交一次本地进度，这个 200 是内部工程批量值而非平台限额。

```python
from itertools import islice

def batched_units(iterator, size=200):
    while True:
        batch = tuple(islice(iterator, size))
        if not batch:
            return
        yield batch
```

每批校验只读场景、预算币种及每份素材的准备路径；源权限失效但原文件有效可 PREPARING，不能调用上传。按实际受保护前缀生成三级名字，检测同账户计划内重名；从实际提交单元计算数量和预算。保存全部排除行及原因，不自动删行。每批只持久化 bounded rows，正文、组、源映射记录不可依赖动态默认值。完成时锁定草稿、验证 revision 未变，再以稳定单元次序计算摘要并将预览置 FROZEN；变更则 OBSOLETE。进程重启复用已持久化抽样与同批次号，从唯一键缺口续写。

补充真实 PostgreSQL 测试：同一 revision 重复生成返回同预览；生成期间修改草稿得到 OBSOLETE；2剧×3户×3组×2SP=6/18/36；币种不匹配与单户 Minis 错误只排除对应组合；同组跨账户正文完全一致；预览后新增素材不进快照；Spy 断言从未调用 ensure_target_asset 或任何 TikTok create。
- [x] **Step 5: 绿测并提交。** `uv run alembic upgrade head`；`uv run pytest tests/modules/builds/test_previews.py -q`，预期全部通过且无外部写操作。`git add backend/app/modules/builds backend/tests/modules/builds backend/app/alembic/versions/0005_strategies_build_previews.py`；`git commit -m "builds: freeze paginated full-account previews"`。

### Task 6: shadcn 策略编辑、粘贴输入与预览交互

**Files:**
- Create: `frontend/src/features/strategies/{StrategyForm,StrategyVersionList}.tsx`。
- Create: `frontend/src/features/builds/{BuildDraftForm,MaterialGroupsEditor,BuildPreviewPanel}.tsx`、`api.ts`。
- Create: `frontend/src/routes/_layout/tenants.$tenantId.strategies.tsx`、`tenants.$tenantId.builds.new.tsx`、`tenants.$tenantId.build-drafts.$draftId.tsx`、`tenants.$tenantId.build-previews.$previewId.tsx`。
- Create/Modify: `backend/app/modules/builds/api.py`、`backend/app/api/main.py`；使用计划 01 的 OpenAPI 客户端生成命令更新客户端。
- Test: `frontend/tests/strategies.spec.ts`、`frontend/tests/build-preview.spec.ts`、`backend/tests/modules/builds/test_api.py`。

**Interfaces:**
- Consumes: 前五个 Task 的租户 API，计划 01 的 TenantContext 页面上下文和生成客户端。
- Produces: `BuildPreviewPanel({preview, onSubmit}: {preview: PreviewSummary, onSubmit: () => void})`；`PreviewSummary` 包含 `id/revision/status/campaign_count/adgroup_count/ad_count/excluded_count/currency/daily_budget_sum/reasons`；计划 06 连接 onSubmit，不在本计划加入假执行器。

API请求与返回固定：POST `/build-drafts` 接收create_draft接口的同名业务字段，返回`{draft_id,revision:1}`；POST `/{draft_id}/prepare` 接收`{request_id}`返回`{task_id}`；POST `/{draft_id}/preview`接收`{expected_revision}`返回`{preview_id}`；GET `/build-previews/{preview_id}` 返回PreviewSummary。生成预览是异步操作，POST不直接返回完整摘要。

- [ ] **Step 1: 写粘贴输入与排除项浏览器红测。** 以计划 01 的登录 fixture 和 mock API 返回冻结预览；`installPreviewFixture(page)` 在本测试文件建立 route mock，返回 2 剧、3 账户、1 排除组合的固定结构。

```typescript
import { test, expect } from "@playwright/test";

const tenantId = "1fe39141-fd11-4077-8aeb-4956ec460651";
const previewId = "7654c5c6-a20f-4c57-9461-97969ce06ca6";

test("pasted inputs produce a visible partial preview without reselection", async ({ page }) => {
  await page.route("**/api/tenants/*/build-drafts/*/preview", route => route.fulfill({
    json: { preview_id: previewId }
  }));
  await page.route(`**/api/tenants/${tenantId}/build-previews/${previewId}`, route => route.fulfill({
    json: { id: previewId, revision: 1, status: "FROZEN", campaign_count: 5,
      adgroup_count: 15, ad_count: 30, excluded_count: 1, currency: "USD",
      daily_budget_sum: "500.00", reasons: ["账户 C 无当前 Minis 权限"] }
  }));
  await page.goto(`/tenants/${tenantId}/builds/new`);
  await page.getByRole("combobox", { name: "版权方连接" }).click();
  await page.getByRole("option", { name: "网眼" }).click();
  await page.getByRole("combobox", { name: "投放策略" }).click();
  await page.getByRole("option", { name: "普通短剧" }).click();
  await page.getByLabel("剧目名称", { exact: true }).fill("The Bond\nNew Story");
  await page.getByLabel("广告账户", { exact: true }).fill("A\nB\nC");
  await page.getByRole("button", { name: /解析并准备/ }).click();
  await expect(page.getByRole("button", { name: /生成搭建预览/ })).toBeEnabled();
  await page.getByRole("button", { name: /生成搭建预览/ }).click();
  await page.getByRole("button", { name: /排除项/ }).click();
  await expect(page.getByText("账户 C 无当前 Minis 权限")).toBeVisible();
  await expect(page.getByRole("button", { name: "创建并立即启用 5 个 Campaign / 15 个 Ad Group / 30 条 Ad" })).toBeEnabled();
  await expect(page.getByRole("checkbox", { name: /选择剧目|选择账户/ })).toHaveCount(0);
});
```

测试 fixture 同时拦截策略/版权方/应用列表、草稿create/prepare、准备结果分页与预览读取API；prepare 的读取响应须从处理中推进到可生成预览，排除项分页须包含示例原因：固定草稿ID `d0eaee6e-f20d-44aa-9303-6f189df5f4be`、准备任务ID `21981379-5dcb-4d03-89c4-d1557cd8b8e3`，按上述接口返回值构造route。列表响应使用Page(items,next_cursor=null)，策略/版权方选择值使用fixture中的已授权记录；登录沿用计划01认证fixture，不发送真实业务请求。
- [ ] **Step 2: 红测。** `bunx playwright test tests/strategies.spec.ts tests/build-preview.spec.ts`，预期新路由不存在或找不到控件。
- [ ] **Step 3: 实现确定的预览动作组件。** 使用仓库内 shadcn Button/Alert/Table/Input/Textarea；只对版权方、应用和策略提供 Select，输入剧目与账户始终是多行 Textarea。

```tsx
import { Button } from "@/components/ui/button";

type PreviewSummary = {
  id: string; revision: number; status: "BUILDING" | "FROZEN" | "OBSOLETE";
  campaign_count: number; adgroup_count: number; ad_count: number;
  excluded_count: number; currency: string; daily_budget_sum: string; reasons: string[];
};

export function BuildPreviewPanel({ preview, onSubmit }: {
  preview: PreviewSummary; onSubmit: () => void;
}) {
  const ready = preview.status === "FROZEN" && preview.campaign_count > 0;
  return <section aria-label="搭建预览">
    <p>本次排除 {preview.excluded_count} 个组合</p>
    <ul>{preview.reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul>
    <p>配置日预算合计：{preview.currency} {preview.daily_budget_sum}</p>
    <Button disabled={!ready} onClick={onSubmit}>
      创建并立即启用 {preview.campaign_count} 个 Campaign / {preview.adgroup_count} 个 Ad Group / {preview.ad_count} 条 Ad
    </Button>
  </section>;
}
```

- [ ] **Step 4: 接通草稿 API、分页和编辑失效。** 草稿保存返回 revision；生成预览返回 ID 后轮询头状态，表格按 cursor 加载。素材编辑含删除/补选/组间移动，但不要求逐项确认自动命中。修改后立即让旧预览按钮不可提交，并调用 PATCH 的 expected_revision；409 显示最新版本并保留用户文本。策略编辑显示 Campaign 预算共享说明、目标 ROAS、每组素材数量与创意数量，展示 100 条英文文案池版本和官方 CTA 来源说明。切租户取消旧请求、清理查询缓存。补上 Playwright 用例：更改素材后旧预览不可提交、复制版本不改旧版本、三层名称预览、空素材可见排除、没有第二次勾选表格。

**UI-03 / UI-11 页面实现与验收细化（前端补充草案）**

依据[前端设计第4～8节](../specs/2026-09-08-tiktok-06-frontend-experience-design.md)和[原型](../prototypes/2026-09-08-tiktok-workbench.html)，Step 3/4 的预览组件只是提交动作接口示例，不能代替完整页面。策略 UI-06/07 另见本 Task 的策略补充。

- [ ] 拆分 `BuildInputPage`、`BuildPreparationPage`、`PreviewWorkspace`、`DramaMaterialSheet`、`PreviewSummaryBar`、`PreviewUnitTable`、`PreviewExclusions`。三阶段共用草稿上下文，UI-11 有独立预览URL；任务详情由P06负责。
- [ ] UI-03 输入：版权方/应用/策略字段，剧目和账户并列多行框；主按钮“解析并准备”，副按钮“保存草稿”。准备页用剧目与素材/账户解析/输入问题 Tabs，保留原始行号；有效项不需再次勾选。
- [ ] 素材 Sheet 明示“用于本批该剧全部账户”，增减/组间移动只改草稿；默认排序、尾组和多剧命中提示遵循既有规则。保存更新 revision；409 保留本地编辑并展示最新版本，避免覆盖。
- [ ] UI-11 默认剧目汇总，按需取分组/冻结创意；账户组合与排除项服务端 cursor 分页，默认50条可选100；摘要来自完整后台汇总，不用当前页计数。读取接口提供剧目汇总、组合、分组、排除项所需分页视图，遵循现有 Page 契约，不把全部组合加载前端。
- [ ] 固定底栏并列三级实际提交数量、排除数量、币种和配置日预算合计；“创建并立即启用”的可访问名称包含数量，无第二激活流程。预览过期、全阻断、生成中不能提交；合法素材待分发可提交。
- [ ] 页面状态用 S01～S07：初始/过滤空、准备进度、行错误、部分阻断、过期/409、网络错误、401/403。区分未解析输入行和已形成但排除的组合，不混算对象数。
- [ ] 增补浏览器用例：2剧×3户默认6 Campaign；任一剧素材侧栏修改影响该剧全部账户；N从2改3只改变Ad数，日预算不倍增；跨页过滤不改变提交范围；全阻断禁用；素材编辑后旧预览失效；409不丢文本。对应真实接口测试，原型不作为通过应用验收的证据。

- [ ] **Step 5: 运行绿测与构建。** `uv run pytest tests/modules/strategies tests/modules/builds/test_drafts.py tests/modules/builds/test_previews.py tests/modules/builds/test_api.py -q`；`bunx playwright test tests/strategies.spec.ts tests/build-preview.spec.ts`；`bun run build`。预期全部通过；没有广告执行能力前由页面容器注入测试回调，计划 06 实现真实提交连接。
- [ ] **Step 6: 提交本 Task。** `git add backend/app/modules/builds/api.py backend/app/api/main.py backend/tests/modules/builds frontend/src/features frontend/src/routes frontend/tests`；`git commit -m "builds: add strategy and batch preview workspace"`。

#### 前端交互补充草案，待本轮评审：UI-06 / UI-07 策略页面

本补充只细化 Task 6 的策略 UI，按[策略设计第 11 节](../specs/2026-09-08-tiktok-04-strategies-design.md)和[全局前端体验设计](../specs/2026-09-08-tiktok-06-frontend-experience-design.md)执行；不修改本 Task 的批量粘贴、资源准备和核心预览流程。

**Files：**

- Create: `frontend/src/features/strategies/StrategyList.tsx`、`StrategyStructureExample.tsx`、`StrategyNamingExample.tsx`、`CopyPoolSheet.tsx`。
- Modify: `frontend/src/features/strategies/StrategyForm.tsx`、`StrategyVersionList.tsx`、`frontend/tests/strategies.spec.ts`。
- Create: `frontend/src/routes/_layout/tenants.$tenantId.strategies.index.tsx`、`tenants.$tenantId.strategies.new.tsx`、`tenants.$tenantId.strategies.$strategyId.tsx`。
- Modify: `frontend/src/routes/_layout/tenants.$tenantId.strategies.tsx` 作为父路由，只渲染 Outlet，避免列表与编辑页叠加；不修改 builds 路由。

**Interfaces：** 页面路由为 `/tenants/:tenantId/strategies`、`/tenants/:tenantId/strategies/new`、`/tenants/:tenantId/strategies/:strategyId`。表单消费 Task 1 的 `StrategyConfig`，名称作为已有独立字段传入创建服务；字段为 budget、currency、target_roas、group_size、creative_count、copy_pool_version、cta_option_ids、campaign_suffix，不增加账户池或第二套 SP 模式。API DTO 仍由生成客户端提供。

- [ ] **UI-S1：实现列表、历史和权限。** UI-06 显示策略名称、当前版本、Campaign 日预算、ROAS、每组素材、创意数量、状态、更新信息和操作列；服务端筛选与游标分页默认 50，可切 100。创建按钮打开 new 路由；复制只预填新建表单，保存前不写入新策略；停用只修改策略可用性，已提交任务不受影响。viewer 只读，隐藏所有策略写操作。
- [ ] **UI-S2：实现独立分区表单。** 使用 FieldGroup/Field/FieldLabel/FieldDescription/FieldError，分区对应基本信息、预算与出价、素材与创意、文案与 CTA、广告命名、只读生成规则。保持全局浅色主题与紧凑控件，不用多层弹窗完成策略编辑。新建主按钮为“创建策略”，编辑为“保存为新版本”；表单没有变化时不创建新版本。
- [ ] **UI-S3：实现不影响配置的即时结构示例。** 新建 `StrategyStructureExample.tsx`，使用固定 23 条示例素材和一个剧目、一个账户；示例数据不写入 StrategyConfig。预算保持字符串，结构整数运算不参与金额计算。

```tsx
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"

type StrategyStructureExampleProps = {
  groupSize: number
  creativeCount: number
  budget: string
  currency: string
}

export function StrategyStructureExample({ groupSize, creativeCount, budget, currency }: StrategyStructureExampleProps) {
  const valid = Number.isInteger(groupSize) && groupSize > 0
    && Number.isInteger(creativeCount) && creativeCount > 0
  const groups = valid ? Math.ceil(23 / groupSize) : null
  return <Card role="region" aria-label="结构与预算示例">
    <CardHeader>
      <CardTitle>结构与预算示例</CardTitle>
      <CardDescription>示例：1 部剧 × 1 个账户，23 条素材</CardDescription>
    </CardHeader>
    <CardContent className="flex flex-col gap-2">
      {groups === null ? <p>填写有效的每组素材数量和创意数量后显示结构。</p> : <>
        <p>1 个 Campaign</p>
        <p>{groups} 个 Ad Group</p>
        <p>{groups * creativeCount} 条 Ad</p>
        <p>每组 SP1～SP{creativeCount}：相同素材，不同文案</p>
      </>}
      <p>{budget ? `${currency} ${budget} / Campaign / 天` : "填写 Campaign 日预算后显示金额。"}</p>
      <p>多个广告组共享该 Campaign 的日预算。</p>
    </CardContent>
  </Card>
}
```

表单 integer 控件传入数字，预算控件始终保留十进制字符串；创意数量从 2 改到 3 时只改变结构计数，不乘预算。无效或未填字段不显示伪造的零预算及空广告结构。

- [ ] **UI-S4：接入命名示例、文案池和官方 CTA。** StrategyNamingExample 显示三层名称与不可编辑的归因基础名示例；可编辑字段只有 campaign_suffix，变量按钮只允许 `{YYYYMMDD}`、`{batch_short_id}`。前端即时校验与 Task 3 白名单一致，保存时以后端 validate 为准；不静默截断前缀。CopyPoolSheet 只读展示版本、100 条英文池来源和当前有效数；N 超有效去重容量时定位创意数量字段并禁止保存。CTA 来源于已核实官方候选，保存 cta_option_ids，禁用自由输入；候选未加载时保留已有配置并明确显示状态。
- [ ] **UI-S5：处理加载、保存与版本冲突。** Skeleton/Empty/Alert 分别对应加载、真实空集合和失败，不能互相替代。保存中禁用重复提交并保留字段；明确失败可修正后重试，结果未知先回查策略/版本记录，未证明结果前不再次发送同一保存请求。409 或版本变化保留用户输入并展示服务器版本，不覆盖旧版本或其他用户保存结果。切租户不能将原表单提交到新租户。
- [ ] **UI-S6：补齐策略页面回归并映射设计验收。** 在 `frontend/tests/strategies.spec.ts` 使用 Task 1～3 的测试策略与当前生成 API DTO 设置 route fixtures：配置为 budget="100.00"、currency="USD"、target_roas="1.08"、group_size=10、creative_count=2，文案池有效数 100。这些数值仅为测试数据，不作为生产默认值。

以下用例在已有登录/租户 fixture 和策略 API mock 上执行；策略 ID 为固定测试记录 `294999ce-f767-45c6-8da0-d2cbeaf71963`。

```typescript
test("策略创意数量变化不放大 Campaign 预算", async ({ page }) => {
  await page.goto("/tenants/1fe39141-fd11-4077-8aeb-4956ec460651/strategies/294999ce-f767-45c6-8da0-d2cbeaf71963")
  const example = page.getByRole("region", { name: "结构与预算示例" })
  await expect(example.getByText("6 条 Ad", { exact: true })).toBeVisible()
  await page.getByLabel("创意数量", { exact: true }).fill("3")
  await expect(example.getByText("1 个 Campaign", { exact: true })).toBeVisible()
  await expect(example.getByText("3 个 Ad Group", { exact: true })).toBeVisible()
  await expect(example.getByText("9 条 Ad", { exact: true })).toBeVisible()
  await expect(example.getByText("USD 100.00 / Campaign / 天", { exact: true })).toBeVisible()
  await expect(page.getByLabel("自定义 CTA", { exact: true })).toHaveCount(0)
})
```

| 设计验收 | 本 Task 需要的附加回归 |
| --- | --- |
| UI-S01 | 列表 50/100 分页不加载全部策略；ROAS、币种、K 与 N 对应正确字段 |
| UI-S02 | 上述 23/K10/N2→N3 用例，结构 1/3/6→1/3/9，预算始终 USD 100.00 |
| UI-S03 | 归因基础名示例只读；后缀变化同步三层示例；非法变量定位输入且禁止保存 |
| UI-S04 | 池返回 98 个有效去重正文、N=99 时阻止保存；CTA 控件只包含 fixture 中官方候选 |
| UI-S05 | 保存版本成功后显示实际返回版本号；复制新策略前没有写请求；停用从不请求 TikTok 状态修改 |
| UI-S06 | 模拟确定失败、超时未知、409、空列表及 viewer；分别保留输入、先回查、保留本地编辑、显示 Empty、隐藏写操作 |

- [ ] **UI-S7：运行策略 UI 检查。** `bunx playwright test tests/strategies.spec.ts`、`bun run build`；再执行本 Task 原有 Step 5，确认未影响搭建预览测试。将上述新增策略文件并入本 Task 的同一次完成提交。

## 自检与交接

- [ ] 对照设计核查每剧所有账户、分组尾组、N 条普通 Ad、100 文案及跨账户复用分别由 Task 2/5/6 覆盖。
- [ ] 核查预览期未调用 ensure_target_asset 或 SDK 写接口；准备版权方链接是 Task 4 的独立明确操作。
- [ ] 核查所有输出 DTO/字段与计划 03/04/06 一致，金额为字符串传输、Decimal 运算，业务 ID 不做 JS number。
- [ ] 交付计划 06 所需 `load_frozen_unit/get_frozen_groups/get_preview_units` 及 PreviewSummary；此阶段不宣称广告已经可真实投放。
- [ ] 全局故障、容量与单次真实试投按计划 07 验收；没有开发者应用不阻止完成本计划的离线测试。
