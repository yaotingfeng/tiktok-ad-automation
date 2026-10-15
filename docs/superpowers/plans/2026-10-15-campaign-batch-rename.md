# 推广系列批量改名 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在广告管理里新增"推广系列批量改名"动作，支持查找替换、前缀后缀、表格粘贴三种规则。

**Architecture:** 改名作为广告管理第四种动作（`field="name"`，操作名 `rename`），复用 冻结选择 → 预览 → 提交 → 逐条执行 → 定向刷新 → 操作记录 全链路。改名规则的计算与校验放在独立的 `ad_management/rename.py`，`expansion.py` 只做分派；表格粘贴通过一个新接口把 ID 列表解析成冻结选择，之后走原有预览接口。

**Tech Stack:** FastAPI + SQLModel + Alembic（PostgreSQL）、官方 TikTok Python SDK、React + TypeScript + shadcn/ui、Playwright。

**Spec:** `docs/superpowers/specs/2026-10-15-campaign-batch-rename-design.md`

## 实施中对设计的两处修正（写计划时核对代码发现）

1. **需要数据库迁移**。设计里"预计无需迁移"不成立：`management_capability.operation` 有 CHECK 约束只允许四种操作；`management_preview_item` / `management_task_item` 的 `original_value`、`final_value` 是 `VARCHAR(128)`，而推广系列名称上限 512。
2. **MCP 通道不做单独实现**。`mcp/tool-contracts.json` 里目前没有任何 `management.*` 合同，所有管理写操作在 MCP 通道都返回 `management_contract_unsupported`。改名保持同样行为，等 MCP 管理合同统一登记时再一起支持。

## Global Constraints

- 只改推广系列（`kind == "campaign"`）；仅人工触发，自动规则拒绝 `field="name"`。
- 新名称：去首尾空白后非空、≤512 字符、不含表情符号；同账户内不得与其他推广系列现有名称或本批其他新名称重复。
- 表格粘贴单次 ≤500 行；Tab 或逗号分隔；首行第一列不是纯数字视为表头。
- 查找替换区分大小写、替换所有出现；`replace_with` 可为空字符串。
- 前缀后缀至少填一个；名称已以该前缀开头（或该后缀结尾）则不重复添加。
- 写入请求体只含 `advertiser_id`、`campaign_id`、`campaign_name`。
- 代码注释用中文说明业务规则；不保留兼容旧行为的分支；前端只用 shadcn/ui。
- Git：只暂存本任务文件，禁止 `git add .`；工作区里不属于本任务的改动保持原样。

## Review Focus

1. 名称首尾有空格、全角空格或换行（从飞书复制常见）——应去掉首尾空白后再比较和写入，比较"不变"时也用去空白后的值。→ Task 3 `test_rename_trims_whitespace`
2. 表格里粘贴的 ID 带空格或是科学计数法（Excel 把长数字变成 `1.7E+18`）——应判为"ID 格式无效"而不是"找不到"，提示用户改成文本格式。→ Task 4 `test_resolve_rejects_scientific_notation_id`
3. 查找替换后多个推广系列得到同一个新名字（如都把日期删掉）——应判批内重名，全部标为不可提交，不能只放行第一条。→ Task 3 `test_rename_batch_duplicates_all_rejected`
4. 提交前 TikTok 后台名称已被别人改过——执行时冻结原名与目录当前名不一致，应标为冲突不发送。→ Task 5 `test_rename_skips_when_name_changed`
5. 对改名任务点"恢复"——应明确拒绝并提示，不能把名称误当成预算/ROAS 恢复。→ Task 5 `test_restore_rejects_rename_task`

---

## File Structure

| 文件 | 职责 |
| --- | --- |
| `backend/app/integrations/tiktok/contracts/management.py` | 增加 `name` 字段与 `rename` 操作、命令校验 |
| `backend/app/integrations/tiktok/official/authorization.py` | 广告管理 scope(2) 授予 `rename` |
| `backend/app/modules/accounts/management_capabilities.py` | `rename` 只对应 `campaign` |
| `backend/app/modules/accounts/capabilities.py` | 官方通道能力一律按已保存 scope 推导 |
| `backend/app/modules/accounts/management_capability_models.py` | CHECK 约束加入 `rename` |
| `backend/app/modules/ad_management/models.py` | 两个值列放宽到 512 |
| `backend/app/alembic/versions/0032_management_rename.py` | 新迁移 |
| `backend/app/modules/ad_management/schemas.py` | `RenameRule`、`MutationSpec`/预览项/任务摘要扩展、粘贴解析请求响应 |
| `backend/app/modules/ad_management/rename.py`（新建） | 名称计算、名称校验、改名展开、粘贴 ID 解析 |
| `backend/app/modules/ad_management/expansion.py` | `field=="name"` 分派到 `rename.expand_rename`；抽出能力核验函数 |
| `backend/app/modules/reporting/selection.py` | 新增共享函数 `create_target_selection` |
| `backend/app/modules/automation_rules/executor.py` | 改用 `create_target_selection` |
| `backend/app/modules/ad_management/api.py` | 新接口 `POST /ad-management-rename-selections` |
| `backend/app/modules/ad_management/execution.py`、`submissions.py`、`actions.py` | 执行映射、原值核对、提交门禁、操作记录筛选、恢复拒绝 |
| `backend/app/integrations/tiktok/adapters/sdk_management.py` | 改名请求体与方法分流 |
| `backend/app/modules/automation_rules/*` | 拒绝 `name` |
| `frontend/src/features/ad-management/RenameFields.tsx`（新建） | 三个页签、说明文案、表格解析 |
| `frontend/src/features/ad-management/ManagementPreviewSheet.tsx`、`presentation.ts` | 改名动作、预览列、标签 |
| `frontend/src/features/ads/AdsTableToolbar.tsx`、`AdsWorkspace.tsx` | 入口按钮 |
| `frontend/tests/ad-management.spec.ts` | 端到端用例 |

---

### Task 1: 合同扩展

**Files:**
- Modify: `backend/app/integrations/tiktok/contracts/management.py`
- Modify: `backend/app/modules/ad_management/schemas.py`
- Test: `backend/tests/integrations/tiktok/test_management_contracts.py`（若不存在则新建）、`backend/tests/modules/ad_management/test_schemas_rename.py`（新建）

**Interfaces:**
- Produces:
  - `ManagementField` 含 `"name"`；`_MANAGEMENT_OPERATIONS["name"] == "rename"`；新增公开函数 `operation_for_field(field: str) -> str | None`，供 expansion/execution/submissions 取代各自的映射表。
  - `schemas.RenameRule`：`kind: Literal["replace","affix","explicit"]`、`find: str | None`、`replace_with: str = ""`、`prefix: str = ""`、`suffix: str = ""`、`names: dict[str, str] = {}`。
  - `MutationSpec.value: Decimal | StatusValue | None = None`、`MutationSpec.rename: RenameRule | None = None`。
  - `ManagementTaskMutation.value: Decimal | StatusValue | None = None`、`rename_kind: Literal["replace","affix","explicit"] | None = None`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/modules/ad_management/test_schemas_rename.py
import pytest
from pydantic import ValidationError

from app.modules.ad_management.schemas import MutationSpec, RenameRule


def test_rename_requires_rule_and_set_mode():
    with pytest.raises(ValidationError):
        MutationSpec(field="name", mode="set")
    with pytest.raises(ValidationError):
        MutationSpec(field="name", mode="add", rename=RenameRule(kind="affix", prefix="x"))
    spec = MutationSpec(field="name", mode="set", rename=RenameRule(kind="affix", prefix="【停】"))
    assert spec.value is None


def test_rename_rule_shapes():
    with pytest.raises(ValidationError):
        RenameRule(kind="replace", find="")
    with pytest.raises(ValidationError):
        RenameRule(kind="affix")
    with pytest.raises(ValidationError):
        RenameRule(kind="explicit", names={})
    with pytest.raises(ValidationError):
        RenameRule(kind="explicit", names={str(i): "n" for i in range(501)})
    assert RenameRule(kind="replace", find="0901", replace_with="").replace_with == ""


def test_other_fields_reject_rename_and_require_value():
    with pytest.raises(ValidationError):
        MutationSpec(field="budget", mode="set", value=10, rename=RenameRule(kind="affix", prefix="x"))
    with pytest.raises(ValidationError):
        MutationSpec(field="budget", mode="set")
```

```python
# 追加到 backend/tests/integrations/tiktok/test_management_contracts.py
from uuid import uuid4

import pytest

from app.integrations.tiktok.contracts.ads import EntityRef
from app.integrations.tiktok.contracts.management import ManagementCommand, operation_for_field


def test_rename_command_campaign_only_and_single_key():
    campaign = EntityRef(uuid4(), "adv", "campaign", "c1")
    command = ManagementCommand(campaign, "name", {}, {"campaign_name": "新名"})
    assert command.operation == "rename"
    assert operation_for_field("name") == "rename"
    with pytest.raises(ValueError):
        ManagementCommand(EntityRef(uuid4(), "adv", "adgroup", "g1"), "name", {}, {"campaign_name": "x"})
    with pytest.raises(ValueError):
        ManagementCommand(campaign, "name", {}, {"campaign_name": "x", "budget": "1"})
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/modules/ad_management/test_schemas_rename.py tests/integrations/tiktok/test_management_contracts.py -v`
Expected: FAIL（`RenameRule` 不存在 / `operation_for_field` 不存在）

- [ ] **Step 3: 实现**

`contracts/management.py`：

```python
ManagementField = Literal["roas", "budget", "status", "material_status", "name"]
_MANAGEMENT_OPERATIONS = {
    "roas": "update_roas",
    "budget": "update_budget",
    "status": "set_status",
    "material_status": "set_material_status",
    # 改名只针对推广系列；名称写入与预算共用平台的系列更新接口。
    "name": "rename",
}


def operation_for_field(field: str) -> str | None:
    """字段到管理操作的唯一映射；预览、提交、执行都从这里取，避免多份映射表漂移。"""
    return _MANAGEMENT_OPERATIONS.get(field)
```

在 `ManagementCommand.__post_init__` 末尾追加：

```python
        if self.field == "name":
            # 改名命令只携带名称本身，防止借改名夹带其它字段写入。
            if self.ref.kind != "campaign":
                raise ValueError("rename requires campaign identity")
            if set(self.desired) != {"campaign_name"}:
                raise ValueError("rename requires exactly campaign_name")
            _text(self.desired["campaign_name"], "campaign name")
```

注意 `_text` 限 255 字符，名称上限 512：改为单独校验 `type(...) is str and 0 < len(value.strip()) <= 512`。

`schemas.py`：

```python
MutationField = Literal["roas", "budget", "status", "name"]
RenameKind = Literal["replace", "affix", "explicit"]
MAX_RENAME_ROWS = 500


class RenameRule(BaseModel):
    """改名规则；名称在预览时按规则逐条算出并冻结，重试不再重新计算。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: RenameKind
    find: str | None = None
    replace_with: str = ""
    prefix: str = ""
    suffix: str = ""
    # 表格粘贴：推广系列 ID → 新名称。
    names: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_kind(self) -> RenameRule:
        if self.kind == "replace" and not self.find:
            raise ValueError("replace requires find text")
        if self.kind == "affix" and not (self.prefix or self.suffix):
            raise ValueError("affix requires prefix or suffix")
        if self.kind == "explicit" and not 1 <= len(self.names) <= MAX_RENAME_ROWS:
            raise ValueError("explicit rename requires 1-500 rows")
        return self
```

`MutationSpec`：`value: Decimal | StatusValue | None = None`，增加 `rename: RenameRule | None = None`；`normalize_value` 对 `None` 原样返回；`validate_operation` 开头加：

```python
        if self.field == "name":
            if self.mode != "set" or self.rename is None or self.value is not None:
                raise ValueError("rename only supports set with a rename rule")
            if self.min_value is not None or self.max_value is not None:
                raise ValueError("rename does not take bounds")
            return self
        if self.rename is not None:
            raise ValueError("only name field takes a rename rule")
        if self.value is None:
            raise ValueError("management value required")
```

`ManagementTaskMutation`：`value` 改为可空，加 `rename_kind: RenameKind | None = None`。

- [ ] **Step 4: 运行通过**

Run: `cd backend && uv run pytest tests/modules/ad_management/test_schemas_rename.py tests/integrations/tiktok/test_management_contracts.py tests/modules/ad_management -q`
Expected: PASS（含原有广告管理测试，确认 `value` 可空没破坏旧用例）

- [ ] **Step 5: 提交**

```bash
git add backend/app/integrations/tiktok/contracts/management.py backend/app/modules/ad_management/schemas.py backend/tests/modules/ad_management/test_schemas_rename.py backend/tests/integrations/tiktok/test_management_contracts.py
git commit -m "ad-management: 管理合同支持推广系列改名动作"
```

---

### Task 2: 改名权限与数据库迁移

**Files:**
- Modify: `backend/app/integrations/tiktok/official/authorization.py`
- Modify: `backend/app/modules/accounts/management_capabilities.py`
- Modify: `backend/app/modules/accounts/capabilities.py:460-470`
- Modify: `backend/app/modules/accounts/management_capability_models.py:71`
- Modify: `backend/app/modules/ad_management/models.py:119-120,237-238`
- Create: `backend/app/alembic/versions/0032_management_rename.py`
- Test: `backend/tests/modules/accounts/capabilities/test_management_publish.py`、`backend/tests/integrations/tiktok/test_official_authorization.py`（若无则新建）

**Interfaces:**
- Consumes: Task 1 的 `"rename"` 操作名。
- Produces: 拥有广告管理 scope(2) 的官方授权账户发布 `operation="rename", entity_kind="campaign"` 的 `VERIFIED` 能力行。

**前置检查**：先运行 `cd backend && uv run alembic heads`。当前 head `0031_cancel_stacked_dir_runs` 属于工作区里其他人尚未提交的改动；若开始实施时它仍未提交，先停下来告诉用户，不要把它带进本任务的提交。新迁移的 `down_revision` 填实施时的实际 head。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/integrations/tiktok/test_official_authorization.py
from app.integrations.tiktok.official.authorization import management_operations_for_scopes


def test_ads_management_scope_grants_rename():
    assert "rename" in management_operations_for_scopes({2})
    assert "rename" not in management_operations_for_scopes({6})
```

在 `test_management_publish.py` 里仿照已有"旧授权摘要按 scope 推导"的用例，新增：授权 `permission_summary` 里保存的 `management_operations` 不含 `rename`、`scopes` 含 `"2"` 的官方授权，发布后存在 `operation="rename", entity_kind="campaign", state="VERIFIED"` 的能力行（`test_official_publish_derives_rename_from_saved_scopes`）。

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/integrations/tiktok/test_official_authorization.py tests/modules/accounts/capabilities/test_management_publish.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

`authorization.py`：

```python
    # 官方层级 2 覆盖系列的编辑，系列改名与预算修改同属这一层级。
    operations = {"set_status", "update_budget", "update_roas", "rename"}
```

`management_capabilities.py` 的 `_ENTITY_KINDS_BY_OPERATION` 增加 `"rename": ("campaign",)`。

`capabilities.py`：官方通道不再读取授权摘要里保存的操作列表，一律按已保存 scope 推导，这样 scope 映射扩展后存量授权无需重新授权：

```python
    if job.channel == "OFFICIAL_API":
        # 官方通道的管理操作只由官方 scope 唯一映射决定；授权摘要里保存的列表是授权当时的
        # 映射结果，映射扩展（如新增改名）后会过时，因此不读取。
        management = management_operations_for_scopes(
            {int(value) for value in authorization.scopes if str(value).isdigit()}
        )
    else:
        management = frozenset(summary.get("management_operations", ()))
```

`management_capability_models.py` 与 `models.py` 同步：CHECK 约束加入 `'rename'`；两个模型的 `original_value`、`final_value` 改为 `max_length=512`。

迁移 `0032_management_rename.py`：

```python
"""广告管理支持推广系列改名：能力操作约束加入 rename，值列放宽到名称上限 512。"""

import sqlalchemy as sa
from alembic import op

revision = "0032_management_rename"
down_revision = "0031_cancel_stacked_dir_runs"  # 以实施时 alembic heads 为准
branch_labels = None
depends_on = None

_OPS = "'update_roas','update_budget','set_status','set_material_status'"
_TABLES = ("management_preview_item", "management_task_item")


def upgrade() -> None:
    op.drop_constraint("ck_management_capability_operation", "management_capability", type_="check")
    op.create_check_constraint(
        "ck_management_capability_operation",
        "management_capability",
        f"operation IN ({_OPS},'rename')",
    )
    for table in _TABLES:
        for column in ("original_value", "final_value"):
            op.alter_column(table, column, type_=sa.String(512), existing_type=sa.String(128))


def downgrade() -> None:
    # 回退前必须先确认没有改名任务与 rename 能力行，否则约束与截断会失败。
    op.execute("DELETE FROM management_capability WHERE operation = 'rename'")
    op.drop_constraint("ck_management_capability_operation", "management_capability", type_="check")
    op.create_check_constraint(
        "ck_management_capability_operation",
        "management_capability",
        f"operation IN ({_OPS})",
    )
    for table in _TABLES:
        for column in ("original_value", "final_value"):
            op.alter_column(table, column, type_=sa.String(128), existing_type=sa.String(512))
```

- [ ] **Step 4: 迁移与测试通过**

Run: `cd backend && uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head && uv run pytest tests/integrations/tiktok tests/modules/accounts tests/modules/ad_management -q`
Expected: 迁移往返成功，测试 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/integrations/tiktok/official/authorization.py backend/app/modules/accounts/management_capabilities.py backend/app/modules/accounts/capabilities.py backend/app/modules/accounts/management_capability_models.py backend/app/modules/ad_management/models.py backend/app/alembic/versions/0032_management_rename.py backend/tests/integrations/tiktok/test_official_authorization.py backend/tests/modules/accounts/capabilities/test_management_publish.py
git commit -m "accounts: 广告管理授权账户获得推广系列改名能力"
```

---

### Task 3: 改名预览展开

**Files:**
- Create: `backend/app/modules/ad_management/rename.py`
- Modify: `backend/app/modules/ad_management/expansion.py`（抽出能力核验；`_expand` 开头按 `field=="name"` 分派；`_OPERATION_BY_FIELD` 改用 `operation_for_field`）
- Modify: `backend/app/modules/ad_management/schemas.py`（`ManagementItemPublic` 增加 `drama_name: str | None = None`、`provider_label: str | None = None`、`drama_changed: bool | None = None`；`original_value` 类型放宽为 `Decimal | str | None`）
- Modify: `backend/app/modules/ad_management/previews.py`（`_public_item` 对改名项输出字符串原值和剧名提示）
- Test: `backend/tests/modules/ad_management/test_rename_preview.py`（新建）

**Interfaces:**
- Consumes: `RenameRule`、`operation_for_field`、`app.modules.ads.naming.parse_campaign_name`。
- Produces:
  - `rename.compute_name(current: str, rule: RenameRule, remote_id: str) -> str | None`（`None` 表示 explicit 规则里没有该 ID）
  - `rename.name_problem(name: str) -> str | None`，返回 `name_empty` / `name_too_long` / `name_has_emoji` 或 `None`
  - `rename.expand_rename(session, context, *, bc_id, refs, mutation, route, require_capabilities) -> Expansion`
  - `expansion._capability_verified(session, context, *, bc_id, row, operation, route) -> bool`
  - `ExpandedItem` 增加 `drama_name`、`provider_label`、`drama_changed` 三个可选字段（默认 `None`）
- 预览项 `reason` 新增取值：`rename_campaign_only`、`rename_missing`、`name_empty`、`name_too_long`、`name_has_emoji`、`name_duplicate_in_account`、`name_duplicate_in_batch`。

- [ ] **Step 1: 写失败测试**

复用 `test_previews.py` 里的 `_row`、`_cap`、`_set_selection` 与 `management_env` fixture（把这三个辅助函数移到 `tests/modules/ad_management/conftest.py` 供两个文件共用）。

```python
# backend/tests/modules/ad_management/test_rename_preview.py
from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.ad_management.previews import prepare_preview
from app.modules.ad_management.rename import compute_name, name_problem
from app.modules.ad_management.schemas import MutationSpec, RenameRule
from tests.modules.ad_management.conftest import _cap, _row, _set_selection


def _rename(**rule):
    return MutationSpec(field="name", mode="set", rename=RenameRule(**rule))


def test_compute_name_rules():
    replace = RenameRule(kind="replace", find="0901", replace_with="1015")
    assert compute_name("嘉书-剧A-0901-0901", replace, "1") == "嘉书-剧A-1015-1015"
    affix = RenameRule(kind="affix", prefix="【停】")
    assert compute_name("【停】嘉书-剧A", affix, "1") == "【停】嘉书-剧A"
    explicit = RenameRule(kind="explicit", names={"1": "新名"})
    assert compute_name("旧", explicit, "1") == "新名"
    assert compute_name("旧", explicit, "2") is None


def test_name_problems():
    assert name_problem("   ") == "name_empty"
    assert name_problem("a" * 513) == "name_too_long"
    assert name_problem("剧名🔥") == "name_has_emoji"
    assert name_problem("嘉书-剧A") is None


def test_rename_trims_whitespace(session, management_env):
    context, bc, account, route, selection = management_env
    ref = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "c-trim")
    row = _row(ref, connection_id=route.connection_id)
    row.name = "嘉书-剧A"
    session.add(row)
    _cap(session, context, bc, route, account.advertiser_id, "rename", "campaign")
    session.flush()
    _set_selection(selection, [ref])
    p = prepare_preview(session, context, bc.bc_id, selection.id, _rename(kind="explicit", names={"c-trim": " 嘉书-剧A　"}))
    assert p.items[0].execution_result == "NO_CHANGE"


def test_rename_batch_duplicates_all_rejected(session, management_env):
    context, bc, account, route, selection = management_env
    refs = []
    for remote_id, name in (("c1", "嘉书-剧A-0901"), ("c2", "嘉书-剧A-0902")):
        ref = EntityRef(context.tenant_id, account.advertiser_id, "campaign", remote_id)
        row = _row(ref, connection_id=route.connection_id)
        row.name = name
        session.add(row)
        refs.append(ref)
    _cap(session, context, bc, route, account.advertiser_id, "rename", "campaign")
    session.flush()
    _set_selection(selection, refs)
    p = prepare_preview(session, context, bc.bc_id, selection.id, _rename(kind="explicit", names={"c1": "嘉书-剧A", "c2": "嘉书-剧A"}))
    assert {item.reason for item in p.items} == {"name_duplicate_in_batch"}
    assert p.counts.targets == 0


def test_rename_conflicts_with_existing_account_name(session, management_env):
    context, bc, account, route, selection = management_env
    target = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "c-a")
    other = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "c-b")
    for ref, name in ((target, "嘉书-剧A"), (other, "嘉书-剧B")):
        row = _row(ref, connection_id=route.connection_id)
        row.name = name
        session.add(row)
    _cap(session, context, bc, route, account.advertiser_id, "rename", "campaign")
    session.flush()
    _set_selection(selection, [target])
    p = prepare_preview(session, context, bc.bc_id, selection.id, _rename(kind="replace", find="剧A", replace_with="剧B"))
    assert p.items[0].reason == "name_duplicate_in_account"


def test_rename_reports_drama_change_and_non_campaign(session, management_env):
    context, bc, account, route, selection = management_env
    campaign = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "c-d")
    group = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "g-d")
    row = _row(campaign, connection_id=route.connection_id)
    row.name = "嘉书-剧A-0901"
    session.add_all([row, _row(group, parent=campaign, connection_id=route.connection_id)])
    _cap(session, context, bc, route, account.advertiser_id, "rename", "campaign")
    session.flush()
    _set_selection(selection, [campaign, group])
    p = prepare_preview(session, context, bc.bc_id, selection.id, _rename(kind="replace", find="剧A", replace_with="剧C"))
    by_kind = {item.ref.kind: item for item in p.items}
    assert by_kind["campaign"].final_value == "嘉书-剧C-0901"
    assert by_kind["campaign"].drama_name == "剧C" and by_kind["campaign"].drama_changed is True
    assert by_kind["adgroup"].reason == "rename_campaign_only"


def test_rename_without_capability_unsupported(session, management_env):
    context, bc, account, route, selection = management_env
    ref = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "c-cap")
    session.add(_row(ref, connection_id=route.connection_id))
    session.flush()
    _set_selection(selection, [ref])
    p = prepare_preview(session, context, bc.bc_id, selection.id, _rename(kind="affix", suffix="_1015"))
    assert p.items[0].reason == "management_capability_unverified"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/modules/ad_management/test_rename_preview.py -v`
Expected: FAIL（`rename` 模块不存在）

- [ ] **Step 3: 实现**

`expansion.py`：把 `add_item` 里构造 `ManagementCapability` 查询的那段代码原样抽成 `_capability_verified(session, context, *, bc_id, row, operation, route) -> bool`，`add_item` 改为调用它；`_OPERATION_BY_FIELD[field]` 全部改为 `operation_for_field(field)`；`_expand` 在加载选择和去重排除项后、构造任何预算/ROAS 逻辑之前加：

```python
    if mutation.field == "name":
        # 改名逐条计算名称、只作用于推广系列，与数值类动作的父子联动无关，单独展开。
        from app.modules.ad_management.rename import expand_rename

        return expand_rename(
            session, context, bc_id=bc_id, refs=refs, mutation=mutation,
            route=route, require_capabilities=require_capabilities,
        )
```

`rename.py`：

```python
"""推广系列批量改名：按规则逐条算出新名称、校验并冻结为管理命令。

名称在预览阶段一次算定并写入 final_value，提交和重试只消费冻结值。
改名会改变剧名解析结果，从而改变报表按剧归类，因此每条都附带解析后的剧名供界面提示。
"""

import re
import unicodedata
from copy import deepcopy
from typing import Any

from sqlmodel import Session, select

from app.core.context import TenantContext
from app.integrations.tiktok.contracts.ads import EntityRef
from app.integrations.tiktok.contracts.management import ManagementCommand
from app.modules.ad_management.expansion import (
    Expansion,
    ExpandedItem,
    _capability_verified,
    _grouping_revision,
    _object_for_ref,
    _ref_key,
)
from app.modules.ad_management.schemas import MutationSpec, RenameRule
from app.modules.ads.models import AdObject
from app.modules.ads.naming import parse_campaign_name

MAX_NAME_LENGTH = 512
# 平台不接受表情符号；按常见表情所在的 Unicode 区段与表情变体选择符判断。
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️]")


def _clean(name: str) -> str:
    # 飞书/Excel 复制常带全角空格和换行，统一去掉首尾空白后再比较与写入。
    return name.strip()


def compute_name(current: str, rule: RenameRule, remote_id: str) -> str | None:
    if rule.kind == "explicit":
        new = rule.names.get(remote_id)
        return None if new is None else _clean(new)
    if rule.kind == "replace":
        assert rule.find is not None
        return _clean(current.replace(rule.find, rule.replace_with))
    name = current
    if rule.prefix and not name.startswith(rule.prefix):
        name = rule.prefix + name
    if rule.suffix and not name.endswith(rule.suffix):
        name = name + rule.suffix
    return _clean(name)


def name_problem(name: str) -> str | None:
    if not name.strip():
        return "name_empty"
    if len(name) > MAX_NAME_LENGTH:
        return "name_too_long"
    if _EMOJI.search(name):
        return "name_has_emoji"
    return None
```

`expand_rename` 的逻辑（全部写在 `rename.py`）：

1. 对每个 `ref`：非 `campaign` → `ExpandedItem(reason="rename_campaign_only", execution_result="UNSUPPORTED")`；`_object_for_ref` 查不到 → `reason="ad_not_found"`。
2. `new = compute_name(_clean(row.name), rule, row.remote_id)`；`None` → `rename_missing`；`name_problem(new)` 非空 → 对应 reason。
3. 统计 `(advertiser_id, new)` 出现次数，>1 的全部标 `name_duplicate_in_batch`。
4. 对剩余项一次查询同账户其他推广系列名称：`select(AdObject.advertiser_id, AdObject.remote_id, AdObject.name).where(tenant, advertiser_id in ..., kind == "campaign")`，若 `_clean(name) == new` 且 `remote_id` 不在本批 → `name_duplicate_in_account`。
5. `_capability_verified(..., operation="rename")` 为假且 `require_capabilities` → `management_capability_unverified`。
6. `new == _clean(row.name)` → `execution_result="NO_CHANGE"`、`reason="no_change"`。
7. 其余：`ManagementCommand(row.ref, "name", {"ad_type": row.ad_type, **deepcopy(row.configuration or {})}, {"campaign_name": new})`，`ExpandedItem(original_value=_clean(row.name), final_value=new, execution_result="PENDING", capability={...与 add_item 相同的字段..., "operation": "rename", "grouping_revision": _grouping_revision(session, row.ref)})`。
8. 每一项都附 `name=row.name`；可解析出剧名的项附 `drama_name`、`provider_label`，`drama_changed = parse_campaign_name(new) 的 (provider, drama) != parse_campaign_name(row.name) 的`。无法解析时 `drama_name=None, drama_changed=True`。
9. 返回 `Expansion(refs, (), tuple(items), tuple(commands))`。

`previews.py`：`_public_item` 透传 `drama_name`、`provider_label`、`drama_changed`；`original_value` 对字符串也透传（`schemas.ManagementItemPublic.original_value` 已放宽为 `Decimal | str | None`）。

- [ ] **Step 4: 运行通过**

Run: `cd backend && uv run pytest tests/modules/ad_management -q`
Expected: PASS（新用例 + 原有预览用例）

- [ ] **Step 5: 提交**

```bash
git add backend/app/modules/ad_management/rename.py backend/app/modules/ad_management/expansion.py backend/app/modules/ad_management/schemas.py backend/app/modules/ad_management/previews.py backend/tests/modules/ad_management/test_rename_preview.py backend/tests/modules/ad_management/test_previews.py backend/tests/modules/ad_management/conftest.py
git commit -m "ad-management: 改名预览按规则逐条算出名称并校验重名与剧名变化"
```

---

### Task 4: 表格粘贴解析接口与共享冻结选择

**Files:**
- Modify: `backend/app/modules/reporting/selection.py`（新增 `create_target_selection`）
- Modify: `backend/app/modules/automation_rules/executor.py:87-135`（`_selection` 改为调用共享函数）
- Modify: `backend/app/modules/ad_management/rename.py`（新增 `resolve_rename_rows`）
- Modify: `backend/app/modules/ad_management/schemas.py`（请求/响应模型）
- Modify: `backend/app/modules/ad_management/api.py`（新路由）
- Test: `backend/tests/modules/ad_management/test_rename_resolve.py`（新建）、`backend/tests/modules/automation_rules/test_executor.py`（现有用例保持通过）

**Interfaces:**
- Produces:
  - `reporting.selection.create_target_selection(session, *, context, bc_id, refs: list[dict[str, str]], uses: list[dict[str, Any]], filters: dict[str, Any], now: datetime) -> FrozenSelectionRecord`
  - `schemas.RenameRowInput(campaign_id: str, new_name: str)`、`RenameSelectionRequest(bc_id: str, rows: list[RenameRowInput])`（1–500 行）、`RenameRowProblem(campaign_id: str, reason: Literal["id_invalid","not_found","duplicate_campaign_id"])`、`RenameSelectionPublic(selection: FrozenSelection | None, problems: list[RenameRowProblem])`
  - `rename.resolve_rename_rows(session, context, request) -> RenameSelectionPublic`
  - 接口 `POST /tenants/{tenant_id}/ad-management-rename-selections`（`operation_id="ad_management-resolve_rename_selection"`，权限 `ads_manage`）。前端拿返回的 `selection.selection_id` 与 `explicit` 规则调用原预览接口。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/modules/ad_management/test_rename_resolve.py
from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.ad_management.rename import resolve_rename_rows
from app.modules.ad_management.schemas import RenameRowInput, RenameSelectionRequest
from tests.modules.ad_management.conftest import _row


def _request(bc, rows):
    return RenameSelectionRequest(bc_id=bc.bc_id, rows=[RenameRowInput(campaign_id=c, new_name=n) for c, n in rows])


def test_resolve_finds_campaigns_and_reports_problems(session, management_env):
    context, bc, account, route, _ = management_env
    ref = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "1700000000000000001")
    session.add(_row(ref, connection_id=route.connection_id))
    session.flush()
    result = resolve_rename_rows(session, context, _request(bc, [
        ("1700000000000000001", "新名"),
        ("1700000000000000001", "又一个"),
        ("1799999999999999999", "x"),
    ]))
    assert [r.remote_id for r in result.selection.refs] == ["1700000000000000001"]
    assert {(p.campaign_id, p.reason) for p in result.problems} == {
        ("1700000000000000001", "duplicate_campaign_id"),
        ("1799999999999999999", "not_found"),
    }


def test_resolve_rejects_scientific_notation_id(session, management_env):
    context, bc, *_ = management_env
    result = resolve_rename_rows(session, context, _request(bc, [("1.7E+18", "x")]))
    assert result.selection is None
    assert result.problems[0].reason == "id_invalid"


def test_resolve_does_not_cross_bc(session, management_env):
    """其他 BC 的账户下的推广系列即使 ID 正确也判为找不到。"""
    context, bc, _account, route, _ = management_env
    other = EntityRef(context.tenant_id, "account-in-other-bc", "campaign", "1700000000000000002")
    session.add(_row(other, connection_id=route.connection_id))
    session.flush()
    result = resolve_rename_rows(session, context, _request(bc, [("1700000000000000002", "x")]))
    assert result.problems[0].reason == "not_found"
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/modules/ad_management/test_rename_resolve.py -v`
Expected: FAIL

- [ ] **Step 3: 实现**

`selection.py`：把 `automation_rules/executor.py::_selection` 里从 `digest_source` 到 `session.flush()` 的代码原样移入 `create_target_selection`（`advertiser_ids` 从 `refs` 推导），`executor._selection` 改为先 `selection_payload(...)` 再调用它，`filters` 仍由调用方传入。

`rename.resolve_rename_rows`：

1. `require_tenant(..., action="ads_manage")`。
2. 逐行：`campaign_id.strip()` 不是 `str.isdigit()` → `id_invalid`；同一 ID 第二次及以后出现 → `duplicate_campaign_id`（保留第一次）。
3. 本 BC 可读账户：`select(BCAccountAccess.advertiser_id).where(tenant, bc_id, in_bc, authorized, active)`；在这些账户内查 `AdObject(kind="campaign", remote_id in ids)`；未命中 → `not_found`（不区分"不在本 BC"与"不存在"，避免泄露其他 BC 信息）。
4. 命中项交给 `create_target_selection(..., filters={"source": "rename_paste"}, now=datetime.now(UTC))`，再用 `get_frozen_selection` 返回公开对象；全部未命中时 `selection=None`。

`api.py` 新路由仿照 `prepare_preview_route`：`require_tenant(..., action="ads_manage")` → `resolve_rename_rows` → `session.commit()`。

- [ ] **Step 4: 运行通过**

Run: `cd backend && uv run pytest tests/modules/ad_management tests/modules/automation_rules tests/modules/reporting -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/modules/reporting/selection.py backend/app/modules/automation_rules/executor.py backend/app/modules/ad_management/rename.py backend/app/modules/ad_management/schemas.py backend/app/modules/ad_management/api.py backend/tests/modules/ad_management/test_rename_resolve.py
git commit -m "ad-management: 按表格粘贴的推广系列 ID 生成改名选择"
```

---

### Task 5: 提交、执行、操作记录与自动规则

**Files:**
- Modify: `backend/app/modules/ad_management/submissions.py:111-155`（映射改用 `operation_for_field`，允许 `rename`）
- Modify: `backend/app/modules/ad_management/execution.py:61-130,238-310`（命令构造、原值核对）
- Modify: `backend/app/integrations/tiktok/adapters/sdk_management.py:100-205`（改名请求体与方法）
- Modify: `backend/app/modules/ad_management/actions.py`（`TaskActionFilter` 加 `rename`；摘要读 `rename.kind`；恢复拒绝改名任务）
- Modify: 自动规则动作合同（`backend/app/modules/automation_rules/` 中校验动作字段的位置，搜索 `MutationSpec(` 与 `field` 的白名单）
- Test: `backend/tests/integrations/tiktok/test_management_adapters.py`、`backend/tests/modules/ad_management/test_execution.py`、`backend/tests/modules/ad_management/test_task_history.py`、`backend/tests/modules/ad_management/test_api_actions.py`、`backend/tests/modules/automation_rules/test_executor.py`

**Interfaces:**
- Consumes: Task 1–4 全部产出。
- Produces: 改名任务端到端可执行；操作记录 `?action=rename` 可筛选；`ManagementTaskMutation(field="name", mode="set", value=None, rename_kind=...)`。

- [ ] **Step 1: 写失败测试**

`test_management_adapters.py`（仿照已有预算用例的 SDK 替身）：

```python
def test_rename_payload_regular_and_smart_plus():
    from app.integrations.tiktok.adapters.sdk_management import _payload

    ref = EntityRef(uuid4(), "adv", "campaign", "c1")
    regular = ManagementCommand(ref, "name", {"ad_type": "REGULAR"}, {"campaign_name": "新名"})
    assert _payload(regular, smart_plus=False) == {"advertiser_id": "adv", "campaign_id": "c1", "campaign_name": "新名"}
    smart = ManagementCommand(ref, "name", {"ad_type": "SMART_PLUS"}, {"campaign_name": "新名"})
    assert _payload(smart, smart_plus=True) == {"advertiser_id": "adv", "campaign_id": "c1", "campaign_name": "新名"}
```

再加一条：`apply` 普通系列改名调用 `CampaignCreationApi.campaign_update`、Smart+ 调用 `smart_plus_campaign_update`（沿用文件里已有的 SDK 方法替身断言方式）。

`test_execution.py`：
- `test_rename_skips_when_name_changed`：冻结 `original_value="嘉书-剧A"`，执行前把目录 `AdObject.name` 改成 `"嘉书-剧X"`，执行后该项 `execution_result == "CONFLICT"`，SDK 替身未被调用。
- `test_rename_accepted_requests_targeted_refresh`：正常执行后结果 `ACCEPTED`、触发定向刷新（沿用已有预算用例的刷新断言）。

`test_task_history.py`：`test_filter_rename_tasks`：列表 `action="rename"` 只返回改名任务，摘要 `mutation.field == "name"`、`mutation.rename_kind == "affix"`。

`test_api_actions.py`：`test_restore_rejects_rename_task`：对改名任务调用恢复，抛 `DomainError` code `management_restore_unsupported`，提示"改名任务不支持一键恢复，需要时请再提交一次改名"。

`test_executor.py`：`test_rule_action_rejects_name_field`：规则动作字段为 `name` 时保存失败。

另外剧名归组随改名更新由 `tests/modules/ads/test_directory.py` 中已有的 `append_campaign_name_projection` 用例覆盖，确认其中有"剧名变化推进 grouping_revision"的断言；若没有，在该文件补一条。

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/integrations/tiktok/test_management_adapters.py tests/modules/ad_management tests/modules/automation_rules -q`
Expected: FAIL（新增用例）

- [ ] **Step 3: 实现**

`execution.py`：删除 `_FIELD_BY_OPERATION`，用 contract 的反查（在 `contracts/management.py` 加 `field_for_operation(operation) -> str | None`）。`_command` 增加：

```python
    elif field == "name":
        desired = {"campaign_name": final}
```

`_same_frozen_value` 的 `else` 分支里：

```python
        if operation == "rename":
            # 改名的冻结原值是目录名称；名称已被他人修改则不覆盖。
            current = row.name.strip()
        elif operation == "set_status":
```

（冲突异常 `management_original_changed` 已映射为 `CONFLICT`，与预算一致。）

`sdk_management.py`：`_payload` 在 `status` 分支之后加：

```python
    if command.field == "name":
        # 系列更新接口按字段增量更新，改名只发送名称，避免回写其它旧配置。
        return {
            "advertiser_id": ref.advertiser_id,
            "campaign_id": ref.remote_id,
            "campaign_name": command.desired["campaign_name"],
        }
```

`apply` 的方法分流：`field == "name"` 与 budget 的 campaign 分支相同（Smart+ → `smart_plus_campaign_update`，否则 `campaign_update`），把现有 `elif smart_plus:` / `else:` 两行的 campaign 判断复用即可，无需新增分支。

`submissions.py`：`operation = operation_for_field(field)`；`expected_operation` 白名单改为 `MANAGEMENT_OPERATIONS`。

`actions.py`：
- `TaskActionFilter = Literal["enable", "disable", "budget", "roas", "rename"]`；筛选时 `rename` 映射到 `field == "name"`。
- 摘要构造：`ManagementTaskMutation(field=raw["field"], mode=raw["mode"], value=raw.get("value"), rename_kind=(raw.get("rename") or {}).get("kind"))`。
- 恢复入口开头：源任务预览 `mutation.field == "name"` 时抛 `DomainError("management_restore_unsupported", "改名任务不支持一键恢复，需要时请再提交一次改名")`。

自动规则：在动作校验的字段白名单里明确不包含 `name`，并在拒绝处加中文注释"改名只允许人工触发"。

- [ ] **Step 4: 运行全部后端检查**

Run: `cd backend && bash scripts/test.sh && bash scripts/lint.sh`
Expected: 全部 PASS（测试使用真实 PostgreSQL/Redis）

- [ ] **Step 5: 提交**

```bash
git add backend/app/modules/ad_management/submissions.py backend/app/modules/ad_management/execution.py backend/app/integrations/tiktok/adapters/sdk_management.py backend/app/integrations/tiktok/contracts/management.py backend/app/modules/ad_management/actions.py backend/app/modules/automation_rules backend/tests
git commit -m "ad-management: 改名任务可执行、可筛选，自动规则不允许改名"
```

（`backend/app/modules/automation_rules` 与 `backend/tests` 提交前先 `git status` 确认目录下只有本任务的改动，否则改为逐个文件路径。）

---

### Task 6: 前端改名面板与入口

**Files:**
- Regenerate: `frontend/openapi.json`、`frontend/src/client/*`（`bash scripts/generate-client.sh`）
- Create: `frontend/src/features/ad-management/RenameFields.tsx`
- Modify: `frontend/src/features/ad-management/ManagementPreviewSheet.tsx`
- Modify: `frontend/src/features/ad-management/presentation.ts`
- Modify: `frontend/src/features/ad-management/ManagementTasksPage.tsx`（动作筛选加"改名"）
- Modify: `frontend/src/features/ads/AdsTableToolbar.tsx`、`frontend/src/features/ads/AdsWorkspace.tsx`
- Test: `frontend/tests/ad-management.spec.ts`

**Interfaces:**
- Consumes: 预览接口（`mutation.field="name"`、`mutation.rename`）、`AdManagementService.resolveRenameSelection`、预览项新增 `drama_name` / `provider_label` / `drama_changed`。
- Produces:
  - `ManagementAction` 增加 `{ field: "name"; entry: "selection" | "paste" }`。
  - `RenameFields.tsx` 导出 `parsePastedRows(text: string): { rows: Array<{ campaignId: string; newName: string }>; errors: string[] }` 与 `<RenameFields value onChange />`。

- [ ] **Step 1: 重新生成客户端**

Run: `bash scripts/generate-client.sh`
Expected: `src/client` 出现 `RenameRule`、`resolveRenameSelection`

- [ ] **Step 2: 写失败的端到端用例**

在 `tests/ad-management.spec.ts` 的 `managementBoundary` 中增加对 `/ad-management-rename-selections` 的拦截（返回一条命中、一条 `not_found`），预览拦截按 `body.mutation.field === "name"` 返回含 `drama_changed: true` 的项。新增：

```ts
test("推广系列勾选后批量改名，前缀生成预览并提交", async ({ page }) => {
  const { previewBodies } = await managementBoundary(page)
  await page.goto(`/ads?dimension=campaign`)
  await page.getByRole("checkbox", { name: /选择 campaign-1/ }).check()
  await page.getByRole("button", { name: "批量改名" }).click()
  await page.getByRole("tab", { name: "前缀后缀" }).click()
  await expect(page.getByText("在名称最前面或最后面加上文字")).toBeVisible()
  await page.getByLabel("前缀").fill("【停】")
  await page.getByRole("button", { name: "生成预览" }).click()
  await expect(page.getByText("报表中将归到")).toBeVisible()
  expect(previewBodies.at(-1)?.mutation).toMatchObject({
    field: "name",
    mode: "set",
    rename: { kind: "affix", prefix: "【停】" },
  })
})

test("按表格改名：粘贴两列，找不到的 ID 标为异常", async ({ page }) => {
  await managementBoundary(page)
  await page.goto(`/ads?dimension=campaign`)
  await page.getByRole("button", { name: "按表格改名" }).click()
  await page
    .getByLabel("粘贴推广系列 ID 和新名称")
    .fill("推广系列ID\t新名称\n1700000000000000001\t嘉书-剧A-1015\n1799999999999999999\tx")
  await page.getByRole("button", { name: "生成预览" }).click()
  await expect(page.getByText("1799999999999999999")).toBeVisible()
  await expect(page.getByText("未找到该推广系列")).toBeVisible()
})
```

同文件补纯函数用例（Playwright 中直接 import）：`parsePastedRows` 忽略表头、支持 Tab 与逗号、超过 500 行返回错误"单次最多 500 行，请分批粘贴"。

Run: `cd frontend && bunx playwright test tests/ad-management.spec.ts`
Expected: FAIL

- [ ] **Step 3: 实现**

- `AdsTableToolbar.tsx`：推广系列维度的 `DIMENSION_ACTIONS.campaign` 加 `rename`（标签"批量改名"，`action: { field: "name", entry: "selection" }`）；推广系列维度工具栏常驻一个"按表格改名"按钮（只读成员不显示），触发 `{ field: "name", entry: "paste" }`。
- `AdsWorkspace.tsx`：`entry === "paste"` 时不冻结列表选择，直接打开面板；`entry === "selection"` 走现有 `startManagementAction`。
- `RenameFields.tsx`：shadcn `Tabs` 三个页签，每个页签顶部 `Alert`（`Info` 图标）放设计文档里的说明和示例；表格粘贴用 `Textarea`（`aria-label="粘贴推广系列 ID 和新名称"`），下方显示已识别行数与解析错误。
- `ManagementPreviewSheet.tsx`：`field === "name"` 时显示 `RenameFields`；`paste` 入口先调用 `resolveRenameSelection`，再以返回的 `selection_id` 和 `{ kind: "explicit", names }` 调用预览；解析接口返回的 `problems` 作为不可提交行合并显示在预览表顶部。预览表新增"原名称 / 新名称 / 剧名"列；`drama_changed` 为真时显示 `Badge` "报表中将归到 {drama_name}"，`drama_name` 为空时显示"改名后无法识别剧名，将归入未识别"。顶部汇总"共 N 条：修改 X、跳过 Y、异常 Z"；有异常时提交按钮文案为"仅提交可修改的行"。底部固定提示"改名会同步修改 TikTok 后台的推广系列名称，并影响本工具按剧名的报表归类。"
- `presentation.ts`：`FIELD_LABEL.name = "名称"`；`mutationText` 对 `field === "name"` 返回 `{ replace: "改名 · 查找替换", affix: "改名 · 前缀后缀", explicit: "改名 · 表格粘贴" }[rename_kind]`；新增 `renameReasonLabel` 映射全部 reason 到中文（`name_duplicate_in_batch` → "本批中有同名"，`name_duplicate_in_account` → "账户内已有同名推广系列"，`not_found` → "未找到该推广系列"，`id_invalid` → "ID 格式无效，请把表格列设为文本后重新复制" 等）。
- `actionTitle` 对 `name` 返回"批量改名"。

- [ ] **Step 4: 构建、检查、测试通过**

Run: `cd frontend && bun run lint && bun run build && bunx playwright test tests/ad-management.spec.ts`
Expected: 全部 PASS

- [ ] **Step 5: 提交**

```bash
git add frontend/openapi.json frontend/src/client frontend/src/features/ad-management frontend/src/features/ads/AdsTableToolbar.tsx frontend/src/features/ads/AdsWorkspace.tsx frontend/tests/ad-management.spec.ts
git commit -m "frontend: 推广系列批量改名面板，支持查找替换、前缀后缀与表格粘贴"
```

---

### Task 7: 收尾记录

**Files:**
- Modify: `docs/implementation-progress.md`

- [ ] **Step 1: 全量验证**

Run: `cd backend && bash scripts/test.sh && bash scripts/lint.sh && cd ../frontend && bun run build && bunx playwright test`
Expected: 全部 PASS

- [ ] **Step 2: 记录进度**

在 `docs/implementation-progress.md` 追加本功能的完成任务、提交、测试结果，以及发布注意事项：
- 新迁移 `0032_management_rename`，发布按部署手册先备份、冻结写入。
- 发布后存量官方授权账户需要跑一次账户能力发布才会出现 `rename` 能力行（现有能力发布流程即可，无需重新授权）；发布验收要核对目标账户的 `management_capability` 有 `rename/campaign/VERIFIED`。
- MCP 通道改名与其它管理写操作一样暂不可用。

- [ ] **Step 3: 提交**

```bash
git add docs/implementation-progress.md
git commit -m "docs: 记录推广系列批量改名"
```
