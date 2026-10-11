# 版权方取链参数对齐 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 取链参数与各版权方后台/CLI 行为一致：推广应用保存长期默认值，随剧变化的值按剧自动取，搭建页默认只读、可展开「本次单独调整」。

**Architecture:** `capabilities.py` 成为唯一的参数契约（字段、来源、校验、合并）；`create_preparation_request` 是唯一合并入口，草稿与独立取链页都经过它。按剧自动值在各版权方 detail 阶段逐剧计算并冻结进 effective config。网眼去掉自定义推广名称，超时恢复改为“建链前确认无同参数链接 + 恢复扫描计数”。

**Tech Stack:** FastAPI + SQLModel + Alembic + PostgreSQL（后端，uv）；React + TanStack Query + shadcn/ui + Playwright（前端，Bun）。

**Spec:** `docs/superpowers/specs/2026-10-11-provider-link-config-design.md`

## Global Constraints

- 本仓库 `AGENTS.md` 适用：中文注释说明业务规则；不保留旧行为兼容分支；禁止 `git add .`，只暂存本任务文件；每个任务验证后单独提交，提交信息以 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>` 结尾。
- 开发期不访问真实版权方后台；外部 HTTP 一律用 `httpx.MockTransport`。
- 锁、并发相关测试使用真实 PostgreSQL（现有 `isolated_strategy_database` fixture）。
- Python 4 空格缩进，TS/TSX 2 空格；前端只用 `shadcn/ui` 组件。
- 容量剧集固定第一项、别名 = `aliasList[0] || originalTitle`；网眼建链请求体不得包含 `promote_name`。
- 优先级：本次单独调整 > 按剧自动 > 应用默认 > 系统默认。
- 刚刚好校验：免费集数 0～总集数；免费集数为 0 时跳转集只能 1；跳转集 ≤ max(免费集数, 1)。

## Review Focus

1. 已有草稿在升级后首次「开始准备」：迁移清理后的 `link_config` 必须能通过新校验，不得因残留的旧键或旧默认值报错（Task 2 迁移测试覆盖）。
2. 用户在搭建页清空某个调整字段：前端必须删除该键而不是提交 `null`，后端遇到 `null` 也必须视为未覆盖（Task 1、Task 7 测试覆盖）。
3. 刚刚好应用在发现阶段拉取支付模板失败：连接验证不能因此整体失败，模板为空时付费应用应提示“去重新验证连接”，而不是给出空下拉（Task 3 测试覆盖）。
4. 网眼超时后列表接口延迟可见：0 条时会重新建链；只有完整扫描证明无同参数链接才允许，扫描不完整时保持 `result_unknown`（Task 6 测试覆盖）。
5. 候选手动选择后的兑吧/刚刚好/容量条目：必须进入各自 detail 阶段，而不是直接 `lookup` 导致 `provider_state_invalid`（Task 4 测试覆盖，这是现有缺陷）。

---

## File Structure

| 文件 | 职责 |
| --- | --- |
| `backend/app/modules/providers/capabilities.py` | 参数契约：字段定义、来源、选项、`validate_link_values`、`effective_link_config` |
| `backend/app/modules/providers/models.py` | `ProviderApplication.link_defaults` |
| `backend/app/alembic/versions/0021_provider_link_defaults.py` | 新列 + 未提交草稿清理 |
| `backend/app/modules/providers/router.py` | `PATCH .../link-defaults`；应用列表返回默认值 |
| `backend/app/modules/providers/schemas.py` | `DramaCandidate` 剧事实字段、`ProviderApplicationPublic.link_defaults`、`LinkDefaultsUpdate` |
| `backend/app/modules/providers/repository.py` | 合并入口；候选选择进入 detail 阶段 |
| `backend/app/modules/builds/drafts.py` | 草稿准备使用 effective config |
| `backend/app/modules/providers/adapters/{duiba,gangganhao,rongliang,wangyan}.py` | 剧事实、支付模板发现、别名、去推广名称 |
| `backend/app/modules/providers/link_steps.py` | detail 阶段按剧合并；阶段辅助函数 |
| `backend/app/modules/providers/wangyan_steps.py` | 新恢复逻辑 |
| `frontend/src/features/builds/ProviderLinkConfigFields.tsx` | 字段编辑（默认值/本次调整两种模式） |
| `frontend/src/features/builds/LinkConfigSummary.tsx` | 生效参数只读摘要 |
| `frontend/src/features/providers/ApplicationLinkDefaults.tsx` | 应用默认值对话框 |

---

### Task 1: 参数契约重写

**Files:**
- Modify: `backend/app/modules/providers/capabilities.py`（整文件重写）
- Modify: `backend/app/core/errors.py`（注册新错误码）
- Modify: `backend/tests/modules/providers/test_contracts.py:201-233`
- Create: `backend/tests/modules/providers/test_link_config.py`

**Interfaces:**
- Produces:
  - `LinkConfigOption(value: str, label: str)`
  - `LinkConfigField(name, label, type, required: bool, default, options: list[LinkConfigOption], source: Literal["application_default","per_drama"], rule: str | None)`
  - `LinkConfigSchema(provider_kind, fields, defaults: dict, application_defaults: dict, schema_version=2)`
  - `capabilities_for_kind(kind: str, application: Any = None) -> LinkConfigSchema`
  - `validate_link_values(kind: str, application: Any, values: dict, *, target: Literal["defaults","overrides"]) -> dict[str, Any]`
  - `effective_link_config(kind: str, application: Any, overrides: dict) -> dict[str, Any]`
  - `payment_template_required(application: Any) -> bool`

- [ ] **Step 1: 写失败测试** `backend/tests/modules/providers/test_link_config.py`

```python
from types import SimpleNamespace

import pytest

from app.core.errors import DomainError
from app.modules.providers.capabilities import (
    capabilities_for_kind,
    effective_link_config,
    validate_link_values,
)


def app(**channel):
    return SimpleNamespace(channel_config=channel, link_defaults={})


def names(kind, application=None):
    return [f.name for f in capabilities_for_kind(kind, application).fields]


def test_fields_match_provider_backends():
    assert names("jiashu") == ["episode"]
    assert names("wangyan") == ["episode"]
    assert names("duiba") == ["episode", "card_point_episode"]
    assert names("gangganhao", app(delivery_mode="iaa")) == [
        "free_episode_count",
        "episode_seq",
        "payment_template_id",
    ]
    assert names("rongliang") == []


def test_per_drama_fields_have_rule_and_no_default():
    schema = capabilities_for_kind("duiba")
    card = next(f for f in schema.fields if f.name == "card_point_episode")
    assert card.source == "per_drama" and card.default is None
    assert card.rule == "按剧免费集数"
    free = capabilities_for_kind("gangganhao", app()).fields[0]
    assert free.rule == "按剧免费集数，缺失时为 3"


@pytest.mark.parametrize("mode", ["iaa", "iap", "mixed"])
def test_payment_template_options_and_requirement(mode):
    application = app(
        delivery_mode=mode,
        payment_templates=[{"id": "7", "name": "周卡"}],
    )
    field = capabilities_for_kind("gangganhao", application).fields[2]
    assert field.required is (mode in {"iap", "mixed"})
    assert [(o.value, o.label) for o in field.options] == [("7", "周卡")]


def test_defaults_reject_per_drama_and_unknown_keys():
    with pytest.raises(DomainError):
        validate_link_values("duiba", app(), {"card_point_episode": 3}, target="defaults")
    with pytest.raises(DomainError):
        validate_link_values("wangyan", app(), {"promote_name": "x"}, target="overrides")
    with pytest.raises(DomainError):
        validate_link_values("rongliang", app(), {"alias": "x"}, target="overrides")


def test_null_and_blank_values_mean_not_overridden():
    assert validate_link_values(
        "duiba", app(), {"episode": None, "card_point_episode": ""}, target="overrides"
    ) == {}


def test_integer_and_template_validation():
    assert validate_link_values(
        "gangganhao", app(payment_templates=[{"id": "7", "name": "周卡"}]),
        {"free_episode_count": "0", "payment_template_id": "7"},
        target="overrides",
    ) == {"free_episode_count": 0, "payment_template_id": 7}
    for bad in ({"episode_seq": 0}, {"free_episode_count": -1}, {"payment_template_id": "9"}, {"episode_seq": True}):
        with pytest.raises(DomainError):
            validate_link_values("gangganhao", app(), bad, target="overrides")


def test_effective_config_priority():
    application = SimpleNamespace(
        channel_config={"delivery_mode": "iaa"}, link_defaults={"episode": 3}
    )
    assert effective_link_config("duiba", application, {}) == {"episode": 3}
    assert effective_link_config("duiba", application, {"episode": 5, "card_point_episode": 6}) == {
        "episode": 5,
        "card_point_episode": 6,
    }
    assert effective_link_config("jiashu", app(), {}) == {"episode": 1}
    assert effective_link_config("rongliang", app(), {}) == {}
    # 已合并结果再次合并保持不变：草稿与准备请求都会经过合并入口。
    once = effective_link_config("duiba", application, {"card_point_episode": 6})
    assert effective_link_config("duiba", application, once) == once


def test_paid_application_requires_template_after_merge():
    paid = SimpleNamespace(
        channel_config={"delivery_mode": "iap", "payment_templates": [{"id": "7", "name": "周卡"}]},
        link_defaults={},
    )
    with pytest.raises(DomainError) as error:
        effective_link_config("gangganhao", paid, {})
    assert error.value.code == "link_config_template_required"
    paid.link_defaults = {"payment_template_id": 7}
    assert effective_link_config("gangganhao", paid, {})["payment_template_id"] == 7
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/modules/providers/test_link_config.py -q`
Expected: FAIL（`ImportError: cannot import name 'effective_link_config'`）

- [ ] **Step 3: 重写 `capabilities.py`**

```python
"""版权方取链参数契约：字段、来源、校验与合并的唯一实现。

字段只保留版权方后台建链接口实际允许人工选择的参数；内部参数（小程序 ID、
平台、链路类型、剧集内部 ID、别名）由取链流程按后台网页/CLI 规则自动填写。
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import DomainError

FieldSource = Literal["application_default", "per_drama"]


class LinkConfigOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=255)


class LinkConfigField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=255)
    type: Literal["integer", "select"]
    required: bool = False
    default: Any = None
    options: list[LinkConfigOption] = Field(default_factory=list)
    # application_default：可在推广应用保存默认值；per_drama：未调整时逐剧自动取值。
    source: FieldSource
    rule: str | None = None
    minimum: int = 1


class LinkConfigSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_kind: str = Field(min_length=1, max_length=32)
    fields: list[LinkConfigField] = Field(max_length=50)
    defaults: dict[str, Any] = Field(default_factory=dict)
    application_defaults: dict[str, Any] = Field(default_factory=dict)
    schema_version: int = Field(gt=0, strict=True)


def _invalid(message: str = "取链参数无效", code: str = "link_config_invalid") -> DomainError:
    return DomainError(code, message)


def payment_template_required(application: Any) -> bool:
    """刚刚好付费、混合变现的应用建链必须带支付模板，与后台和 CLI 一致。"""

    config = getattr(application, "channel_config", None) or {}
    return str(config.get("delivery_mode", "iaa")).lower() in {"iap", "mixed"}


def _template_options(application: Any) -> list[LinkConfigOption]:
    config = getattr(application, "channel_config", None) or {}
    rows = config.get("payment_templates") or []
    return [
        LinkConfigOption(value=str(row["id"]), label=str(row["name"]))
        for row in rows
        if isinstance(row, dict) and row.get("id") is not None and row.get("name")
    ]


def _fields(kind: str, application: Any) -> list[LinkConfigField]:
    episode = LinkConfigField(
        name="episode", label="起播集", type="integer", default=1, source="application_default"
    )
    if kind in {"jiashu", "wangyan"}:
        return [episode]
    if kind == "duiba":
        return [
            episode,
            LinkConfigField(
                name="card_point_episode",
                label="卡点集",
                type="integer",
                source="per_drama",
                rule="按剧免费集数",
            ),
        ]
    if kind == "gangganhao":
        return [
            LinkConfigField(
                name="free_episode_count",
                label="免费集数",
                type="integer",
                source="per_drama",
                rule="按剧免费集数，缺失时为 3",
                minimum=0,
            ),
            LinkConfigField(
                name="episode_seq",
                label="跳转集",
                type="integer",
                default=1,
                source="application_default",
            ),
            LinkConfigField(
                name="payment_template_id",
                label="支付模板",
                type="select",
                required=payment_template_required(application),
                options=_template_options(application),
                source="application_default",
            ),
        ]
    if kind == "rongliang":
        # 容量后台新增链接只允许选择合集；剧集与别名由网页自动填写，这里保持一致。
        return []
    raise ValueError("unknown provider kind")


def validate_link_values(
    kind: str,
    application: Any,
    values: dict[str, Any],
    *,
    target: Literal["defaults", "overrides"],
) -> dict[str, Any]:
    """校验应用默认值或本次调整；None、空字符串表示未设置，直接丢弃。"""

    if not isinstance(values, dict):
        raise _invalid()
    fields = {field.name: field for field in _fields(kind, application)}
    result: dict[str, Any] = {}
    for name, raw in values.items():
        field = fields.get(name)
        if field is None or (target == "defaults" and field.source != "application_default"):
            raise _invalid(f"不支持的取链参数：{name}")
        if raw is None or raw == "":
            continue
        if isinstance(raw, bool):
            raise _invalid(f"{field.label}必须是整数")
        try:
            number = int(str(raw))
        except ValueError:
            raise _invalid(f"{field.label}必须是整数") from None
        if str(number) != str(raw).strip() or number < field.minimum:
            raise _invalid(f"{field.label}必须是不小于 {field.minimum} 的整数")
        # 下拉字段只接受版权方返回的选项；没有选项时无法核实，直接拒绝。
        if field.type == "select" and str(number) not in {
            option.value for option in field.options
        }:
            raise _invalid(f"{field.label}不在版权方可选范围内")
        result[name] = number
    return result


def effective_link_config(kind: str, application: Any, overrides: dict[str, Any]) -> dict[str, Any]:
    """本次调整覆盖应用默认值，再补系统默认；按剧字段未调整时不写入，留给取链逐剧计算。"""

    fields = _fields(kind, application)
    merged = {
        field.name: field.default
        for field in fields
        if field.source == "application_default" and field.default is not None
    }
    merged.update(
        validate_link_values(
            kind, application, getattr(application, "link_defaults", None) or {}, target="defaults"
        )
    )
    merged.update(validate_link_values(kind, application, overrides, target="overrides"))
    if kind == "gangganhao" and payment_template_required(application) and "payment_template_id" not in merged:
        raise _invalid(
            "付费或混合变现应用需要支付模板，请在版权方连接的推广应用中设置",
            "link_config_template_required",
        )
    return merged


def capabilities_for_kind(provider_kind: str, application: Any = None) -> LinkConfigSchema:
    """Return the safe, UI-facing link configuration contract for a provider."""

    fields = _fields(provider_kind, application)
    return LinkConfigSchema(
        provider_kind=provider_kind,
        fields=fields,
        defaults={field.name: field.default for field in fields if field.default is not None},
        application_defaults=dict(getattr(application, "link_defaults", None) or {}),
        schema_version=2,
    )
```

- [ ] **Step 3b: 注册错误码**（`backend/app/core/errors.py`；未注册的码会被处理器转成 500 `internal_error`，公开文案只取自登记表）

`ERROR_HTTP_STATUS` 增加：

```python
    "link_config_invalid": 422,
    "link_config_template_required": 422,
```

`ERROR_PUBLIC_MESSAGES` 增加：

```python
    "link_config_invalid": "取链参数无效，请检查起播集、卡点集、免费集数、跳转集或支付模板",
    "link_config_template_required": "付费或混合变现应用需要支付模板，请在版权方连接的推广应用中设置",
```

在 `test_link_config.py` 追加：

```python
def test_link_config_errors_are_public_422():
    from app.core.errors import ERROR_HTTP_STATUS, ERROR_PUBLIC_MESSAGES

    for code in ("link_config_invalid", "link_config_template_required"):
        assert ERROR_HTTP_STATUS[code] == 422 and ERROR_PUBLIC_MESSAGES[code]
```

- [ ] **Step 4: 更新 `test_contracts.py` 中两个旧测试**

把 `test_capability_schema_validates_defaults_and_conditional_requirements` 改为：

```python
def test_capability_schema_rejects_unknown_field_keys():
    with pytest.raises(ValueError):
        LinkConfigField(name="Bad-Name", label="x", type="integer", source="per_drama")
    with pytest.raises(ValueError):
        LinkConfigSchema(provider_kind="duiba", fields=[], required_when={}, schema_version=2)
```

把 `test_gangganhao_capability_marks_payment_template_for_paid_modes` 中的 `SimpleNamespace(channel_config=...)` 补上 `link_defaults={}`，其余断言不变。

- [ ] **Step 5: 运行测试**

Run: `cd backend && uv run pytest tests/modules/providers/test_link_config.py tests/modules/providers/test_contracts.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add backend/app/modules/providers/capabilities.py backend/app/core/errors.py backend/tests/modules/providers/test_link_config.py backend/tests/modules/providers/test_contracts.py
git commit -m "providers: 取链参数契约改为来源化字段并集中校验与合并"
```

---

### Task 2: 推广应用默认值存储与迁移

**Files:**
- Modify: `backend/app/modules/providers/models.py:87-91`
- Create: `backend/app/alembic/versions/0021_provider_link_defaults.py`
- Modify: `backend/app/modules/providers/schemas.py:156-160`
- Modify: `backend/app/modules/providers/router.py`（`list_applications`、新增 PATCH）
- Test: `backend/tests/modules/providers/test_preparation_api.py`、Create `backend/tests/migrations/test_0021_provider_link_defaults.py`

**Interfaces:**
- Consumes: `validate_link_values`、`capabilities_for_kind`（Task 1）
- Produces:
  - `ProviderApplication.link_defaults: dict[str, Any]`
  - `ProviderApplicationPublic.link_defaults: dict[str, Any]`
  - `LinkDefaultsUpdate(link_defaults: dict[str, Any])`
  - `PATCH /api/tenants/{tenant_id}/providers/connections/{connection_id}/applications/{application_id}/link-defaults -> LinkConfigSchema`，权限 `manage`，审计 `provider.application.link_defaults.update`

- [ ] **Step 1: 写失败测试**（追加到 `test_preparation_api.py`，复用该文件中 `test_application_capabilities_require_verified_application_and_return_safe_schema` 已有的客户端、连接与应用夹具写法）

```python
def test_application_link_defaults_round_trip_and_survive_rediscovery(
    client, tenant_admin_headers, verified_duiba_application
):
    tenant_id, connection_id = verified_duiba_application
    url = (
        f"/api/tenants/{tenant_id}/providers/connections/"
        f"{connection_id}/applications/external-app/link-defaults"
    )
    response = client.patch(url, headers=tenant_admin_headers, json={"link_defaults": {"episode": 2}})
    assert response.status_code == 200
    assert response.json()["application_defaults"] == {"episode": 2}
    rejected = client.patch(
        url, headers=tenant_admin_headers, json={"link_defaults": {"card_point_episode": 3}}
    )
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "link_config_invalid"
    listed = client.get(
        f"/api/tenants/{tenant_id}/providers/connections/{connection_id}/applications",
        headers=tenant_admin_headers,
    ).json()["items"]
    assert listed[0]["link_defaults"] == {"episode": 2}


def test_operator_cannot_change_application_link_defaults(
    client, operator_headers, verified_duiba_application
):
    tenant_id, connection_id = verified_duiba_application
    response = client.patch(
        f"/api/tenants/{tenant_id}/providers/connections/{connection_id}"
        "/applications/external-app/link-defaults",
        headers=operator_headers,
        json={"link_defaults": {"episode": 2}},
    )
    assert response.status_code == 403
```

如果文件中尚无 `verified_duiba_application` / `operator_headers` 夹具，按同文件 `test_application_capabilities_require_verified_application_and_return_safe_schema` 内的建数逻辑提取成 fixture（`kind="duiba"`、`external_id="external-app"`、`channel_config` 带当前 `verification_token`）。重新发现场景在 `test_connection_isolation.py` 已有的发现测试末尾追加断言：发现前写入 `link_defaults={"episode": 2}`，发现后读取仍为 `{"episode": 2}`。

迁移测试 `backend/tests/migrations/test_0021_provider_link_defaults.py`（若 `tests/migrations` 不存在则创建并加 `__init__.py`；使用仓库已有的 alembic 迁移测试方式，参考 `grep -rn "command.upgrade" backend/tests`）：

```python
def test_0021_cleans_only_unsubmitted_drafts(migrated_to_0020, upgrade_to):
    db = migrated_to_0020
    db.execute(
        "INSERT INTO build_draft (id, tenant_id, status, link_config, ...) VALUES "
        "(:a, :t, 'DRAFT', '{\"episode\":1,\"promote_name\":\"x\",\"card_point_episode\":1,\"free_episode_count\":1}'), "
        "(:b, :t, 'DRAFT', '{\"episode\":3,\"payment_template_id\":\"\",\"alias\":\"y\",\"episodic_drama_id\":\"9\"}'), "
        "(:c, :t, 'READY', '{\"episode\":1,\"promote_name\":\"x\"}')"
    )
    upgrade_to("0021_provider_link_defaults")
    rows = dict(db.execute("SELECT id, link_config FROM build_draft").all())
    assert rows[a] == {}
    assert rows[b] == {"episode": 3}
    assert rows[c] == {"episode": 1, "promote_name": "x"}
```

（`...` 部分按 `build_draft` 的 NOT NULL 列补齐，执行者用 `\d build_draft` 查看实际列。）

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/modules/providers/test_preparation_api.py -k link_defaults -q`
Expected: FAIL（404 或 `link_defaults` 缺失）

- [ ] **Step 3: 模型新增列**（`models.py`，`channel_config` 之后）

```python
    # 管理员维护的取链默认值；与版权方发现写入的 channel_config 分开，重新发现不覆盖。
    link_defaults: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False, server_default="{}")
    )
```

在 `__table_args__` 追加 `CheckConstraint("jsonb_typeof(link_defaults) = 'object'", name="ck_provider_application_link_defaults")`。

- [ ] **Step 4: 迁移 `0021_provider_link_defaults.py`**

```python
"""Store provider application link defaults and clear obsolete draft link keys."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0021_provider_link_defaults"
down_revision = "0020_draft_delivery_mutation"
branch_labels = None
depends_on = None

# 已废弃字段：网眼推广名称、容量剧集与别名，均改为自动处理。
OBSOLETE = ("promote_name", "alias", "episodic_drama_id")
# 与旧界面强制写入的默认值相同的参数，清除后改走应用默认或按剧自动。
OLD_DEFAULTS = {
    "episode": "1",
    "episode_seq": "1",
    "card_point_episode": "1",
    "free_episode_count": "1",
}


def upgrade():
    op.add_column(
        "provider_application",
        sa.Column(
            "link_defaults",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_provider_application_link_defaults",
        "provider_application",
        "jsonb_typeof(link_defaults) = 'object'",
    )
    # 只处理尚未开始准备的草稿；已准备、已完成记录保持历史原样。
    for key in OBSOLETE:
        op.execute(
            sa.text(
                "UPDATE build_draft SET link_config = link_config - :key "
                "WHERE status = 'DRAFT' AND link_config ? :key"
            ).bindparams(key=key)
        )
    for key, value in OLD_DEFAULTS.items():
        op.execute(
            sa.text(
                "UPDATE build_draft SET link_config = link_config - :key "
                "WHERE status = 'DRAFT' AND link_config -> :key IN (to_jsonb(CAST(:num AS int)), to_jsonb(CAST(:num AS text)))"
            ).bindparams(key=key, num=value)
        )
    op.execute(
        "UPDATE build_draft SET link_config = ("
        "SELECT COALESCE(jsonb_object_agg(k, v), '{}'::jsonb) FROM jsonb_each(link_config) AS e(k, v) "
        "WHERE v <> 'null'::jsonb AND v <> '\"\"'::jsonb) WHERE status = 'DRAFT'"
    )


def downgrade():
    # 草稿清理不可逆；下降只移除新列。
    op.drop_constraint("ck_provider_application_link_defaults", "provider_application", type_="check")
    op.drop_column("provider_application", "link_defaults")
```

- [ ] **Step 5: schema 与路由**

`schemas.py`：

```python
class ProviderApplicationPublic(BaseModel):
    external_id: str
    name: str
    tiktok_minis_id: str | None = None
    available: bool
    link_defaults: dict[str, Any] = Field(default_factory=dict)


class LinkDefaultsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    link_defaults: dict[str, Any]
```

`router.py` 的 `list_applications` 构造 `ProviderApplicationPublic` 时增加 `link_defaults=row.link_defaults`。把 `application_capabilities` 中“查应用 + 校验已验证”抽成 `_verified_application(session, tenant_id, connection, application_id) -> ProviderApplication`，两个路由复用。新增：

```python
@router.patch(
    "/connections/{connection_id}/applications/{application_id}/link-defaults",
    response_model=LinkConfigSchema,
)
def patch_application_link_defaults(
    tenant_id: UUID,
    connection_id: UUID,
    application_id: str,
    body: LinkDefaultsUpdate,
    session: SessionDep,
    user: CurrentUser,
) -> LinkConfigSchema:
    """保存推广应用的取链默认值；只接受可设默认的字段，按剧字段不能保存为默认。"""

    context = require_tenant(session, actor_id=user.id, tenant_id=tenant_id, action="manage")
    connection = get_connection(session, context=context, connection_id=connection_id)
    app = _verified_application(session, tenant_id, connection, application_id)
    app.link_defaults = validate_link_values(
        connection.kind, app, body.link_defaults, target="defaults"
    )
    session.add(app)
    session.add(
        AuditEvent(
            tenant_id=tenant_id,
            actor_id=user.id,
            action="provider.application.link_defaults.update",
            target_id=str(app.id),
        )
    )
    schema = capabilities_for_kind(connection.kind, app)
    session.commit()
    return schema
```

- [ ] **Step 6: 运行测试**

Run: `cd backend && uv run alembic upgrade head && uv run pytest tests/modules/providers/test_preparation_api.py tests/modules/providers/test_connection_isolation.py tests/migrations/test_0021_provider_link_defaults.py -q`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add backend/app/modules/providers/models.py backend/app/alembic/versions/0021_provider_link_defaults.py backend/app/modules/providers/schemas.py backend/app/modules/providers/router.py backend/tests/modules/providers/test_preparation_api.py backend/tests/modules/providers/test_connection_isolation.py backend/tests/migrations
git commit -m "providers: 推广应用保存取链默认值并清理草稿中的废弃参数"
```

---

### Task 3: 刚刚好支付模板随应用发现

**Files:**
- Modify: `backend/app/modules/providers/adapters/gangganhao.py:81-111`
- Test: `backend/tests/modules/providers/test_gangganhao_protocol.py`

**Interfaces:**
- Produces: 刚刚好应用 `channel_config["payment_templates"] = [{"id": "7", "name": "周卡"}]`（字符串 ID），供 Task 1 `_template_options` 读取。

- [ ] **Step 1: 写失败测试**

```python
def test_gangganhao_discovery_records_payment_templates_per_application():
    def handle(request):
        if request.url.path.endswith("/apps"):
            return httpx.Response(200, json={"code": 0, "data": {"list": [
                {"authorizerAppId": 16, "appName": "Rovela", "deliveryMode": "iap"},
                {"authorizerAppId": 17, "appName": "Free", "deliveryMode": "iaa"},
            ]}})
        if request.url.path.endswith("/payment-templates"):
            assert request.url.params["authorizerAppId"] == "16"
            return httpx.Response(200, json={"code": 0, "data": {"list": [{"id": 7, "name": "周卡"}]}})
        raise AssertionError(request.url)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        apps = GangganhaoClient(http, token="t").discover_applications()
    assert apps[0]["channel_config"] == {
        "delivery_mode": "iap",
        "payment_templates": [{"id": "7", "name": "周卡"}],
    }
    # 免费应用不需要支付模板，不额外请求。
    assert apps[1]["channel_config"] == {"delivery_mode": "iaa"}


def test_gangganhao_template_fetch_failure_keeps_discovery():
    def handle(request):
        if request.url.path.endswith("/apps"):
            return httpx.Response(200, json={"code": 0, "data": {"list": [
                {"authorizerAppId": 16, "appName": "Rovela", "deliveryMode": "mixed"}
            ]}})
        return httpx.Response(500)

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        apps = GangganhaoClient(http, token="t").discover_applications()
    assert apps[0]["channel_config"] == {"delivery_mode": "mixed", "payment_templates": []}
```

（构造函数签名以文件内 `GangganhaoClient.__init__` 为准；`_request` 的 500 处理以现有实现抛出的 `DomainError` 为准。）

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/modules/providers/test_gangganhao_protocol.py -k templates -q`
Expected: FAIL

- [ ] **Step 3: 实现**（在 `discover_applications` 组装每行 `config` 后）

```python
            if str(config.get("delivery_mode", "iaa")).lower() in {"iap", "mixed"}:
                # 付费/混合应用建链必须选择支付模板；拉取失败不阻断连接验证，
                # 搭建页会提示重新验证连接以刷新模板。
                config["payment_templates"] = self._payment_templates(external)
```

```python
    def _payment_templates(self, authorizer_app_id: str) -> list[JsonDict]:
        try:
            data = self._request(
                "GET", "/payment-templates", query={"authorizerAppId": authorizer_app_id}
            )
        except DomainError:
            return []
        rows = data.get("list") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            return []
        return [
            {"id": str(row["id"]), "name": row["name"]}
            for row in rows
            if isinstance(row, dict)
            and row.get("id") is not None
            and isinstance(row.get("name"), str)
            and row["name"].strip()
        ]
```

- [ ] **Step 4: 运行测试**

Run: `cd backend && uv run pytest tests/modules/providers/test_gangganhao_protocol.py -q`
Expected: PASS（`test_gangganhao_login_apps_search_and_iaa_create` 中 iaa 应用不请求模板，断言保持不变）

- [ ] **Step 5: 提交**

```bash
git add backend/app/modules/providers/adapters/gangganhao.py backend/tests/modules/providers/test_gangganhao_protocol.py
git commit -m "gangganhao: 发现付费应用时记录可选支付模板"
```

---

### Task 4: 合并入口、剧事实与候选选择阶段

**Files:**
- Modify: `backend/app/modules/providers/repository.py:273-320`（合并）、`:535`（阶段）
- Modify: `backend/app/modules/builds/drafts.py:415-500`
- Modify: `backend/app/modules/providers/schemas.py:29-35`
- Modify: `backend/app/modules/providers/link_steps.py`（`_resolved_drama`、新增 `stage_after_resolution`、兑吧/刚刚好 search 结束与 detail 阶段、`ERROR_MESSAGES`、状态映射）
- Modify: `backend/app/modules/providers/adapters/duiba.py:101-127`、`gangganhao.py:113-150`
- Test: `backend/tests/modules/providers/test_new_provider_workflows.py`、`test_duiba_protocol.py`、`test_gangganhao_protocol.py`、`backend/tests/modules/builds/test_drafts.py`

**Interfaces:**
- Consumes: `effective_link_config`（Task 1）、`link_defaults`（Task 2）
- Produces:
  - `DramaCandidate.free_episode_count: int | None`、`DramaCandidate.episode_count: int | None`
  - `stage_after_resolution(kind: str) -> str`（duiba→`duiba_preview`，gangganhao→`ggh_detail`，rongliang→`rl_detail`，其他→`lookup`）
  - 错误码 `link_episode_out_of_range`（状态 `config_conflict`）

- [ ] **Step 1: 写失败测试**

`test_duiba_protocol.py`：

```python
def test_duiba_search_carries_drama_free_episode_count():
    def handle(request):
        return httpx.Response(200, json={"code": 0, "data": {"list": [
            {"id": 31, "title": "Moon", "language": "en", "freeEpisodeCount": 8, "episodeCount": 60}
        ], "total": 1}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        page = DuibaClient(http, token="t").search("Moon", None)
    assert page.items[0].free_episode_count == 8
    assert page.items[0].episode_count == 60
```

`test_gangganhao_protocol.py` 同理断言 `search` 返回 `free_episode_count == 3`、`episode_count == 10`（复用首个测试中的 `/series` 响应）。

`test_new_provider_workflows.py`（沿用该文件 `workflow` 参数化夹具与 `finish` 辅助；以下为新增用例，夹具里剧单行需带 `freeEpisodeCount`）：

```python
def test_duiba_card_point_defaults_to_each_drama_free_count(duiba_workflow):
    item = finish(duiba_workflow)
    assert item.status == "ready", item.resolved
    body = created_body(duiba_workflow)
    assert body["defaultEpisode"] == 1
    assert body["cardPointEpisode"] == 8  # 剧单 freeEpisodeCount


def test_duiba_explicit_card_point_override_wins(duiba_workflow_with_config):
    workflow = duiba_workflow_with_config({"card_point_episode": 5})
    assert finish(workflow).status == "ready"
    assert created_body(workflow)["cardPointEpisode"] == 5


def test_gangganhao_free_count_defaults_to_drama_then_three(ggh_workflow, ggh_workflow_without_free):
    assert finish(ggh_workflow).status == "ready"
    assert created_body(ggh_workflow)["freeEpisodeCount"] == 3
    assert finish(ggh_workflow_without_free).status == "ready"
    assert created_body(ggh_workflow_without_free)["freeEpisodeCount"] == 3


def test_gangganhao_sequence_beyond_drama_free_count_is_config_conflict(ggh_workflow_with_config):
    workflow = ggh_workflow_with_config({"episode_seq": 5})  # 剧免费集数 3
    item = finish(workflow)
    assert item.status == "config_conflict"
    assert item.resolved["error_code"] == "link_episode_out_of_range"
    assert not created_body(workflow, allow_missing=True)


@pytest.mark.parametrize("kind", ["duiba", "gangganhao", "rongliang"])
def test_selected_candidate_enters_provider_detail_stage(ambiguous_workflow, kind):
    workflow = ambiguous_workflow(kind)
    item = finish(workflow)
    assert item.status == "needs_resolution"
    choose_first_candidate(workflow)
    assert finish(workflow).status == "ready"
```

`created_body`、`choose_first_candidate`、按 kind 生成夹具的辅助函数在该文件现有夹具基础上提取：`created_body` 返回 MockTransport 记录中最后一次创建链接 POST 的 JSON（兑吧 `/link/create`、刚刚好 `/campaign-link`）；`choose_first_candidate` 调用 `app.modules.providers.service.choose_drama_candidate`，`external_drama_id` 取 `item.resolved["_work"]["candidates"][0]["external_drama_id"]`；`ambiguous_workflow` 让剧单返回两条同名剧。

`test_drafts.py`：

```python
def test_draft_preparation_uses_application_defaults_and_overrides(draft_with_duiba_application):
    session, context, draft, application = draft_with_duiba_application
    application.link_defaults = {"episode": 2}
    draft.link_config = {"card_point_episode": 6}
    session.add_all([application, draft])
    session.commit()
    prepare_draft(session, context=context, draft_id=draft.id, request_id=uuid4())
    prep = latest_link_preparation(session, draft)
    assert prep.config == {"episode": 2, "card_point_episode": 6}


def test_draft_preparation_rejects_obsolete_override_keys(draft_with_wangyan_application):
    session, context, draft, _ = draft_with_wangyan_application
    draft.link_config = {"promote_name": "x"}
    session.add(draft)
    session.commit()
    with pytest.raises(DomainError) as error:
        prepare_draft(session, context=context, draft_id=draft.id, request_id=uuid4())
    assert error.value.code == "link_config_invalid"
```

（`prepare_draft` 为 `drafts.py` 中调用 `prepare_links` 的现有入口函数名，执行者以 `grep -n "def prepare" backend/app/modules/builds/drafts.py` 确认；夹具参照该测试文件已有草稿建数方式。）

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/modules/providers/test_duiba_protocol.py tests/modules/providers/test_gangganhao_protocol.py tests/modules/providers/test_new_provider_workflows.py tests/modules/builds/test_drafts.py -q`
Expected: FAIL（新断言）

- [ ] **Step 3: `DramaCandidate` 增加剧事实**

```python
class DramaCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_drama_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    language: str | None = None
    display_drama_id: str | None = None
    # 剧单返回的免费集数、总集数，只用于逐剧计算取链参数，不写入剧目表。
    free_episode_count: int | None = Field(default=None, ge=0)
    episode_count: int | None = Field(default=None, ge=1)
```

兑吧 `search` 与刚刚好 `search` 构造 `DramaCandidate` 时增加：

```python
                    free_episode_count=_count(row.get("freeEpisodeCount"), minimum=0),
                    episode_count=_count(row.get("episodeCount"), minimum=1),
```

两个 adapter 各自加私有辅助（或放 `contract.py` 共用，优先共用）：

```python
def count_or_none(value: Any, *, minimum: int) -> int | None:
    """剧单数值字段缺失或格式异常时返回 None，由取链流程按 CLI 规则回退。"""

    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(str(value))
    except ValueError:
        return None
    return number if number >= minimum else None
```

- [ ] **Step 4: `_resolved_drama` 不写入剧事实，阶段辅助函数**

```python
# 剧事实仅存于本次 work，不进入 ProviderDrama 表。
_DRAMA_FACTS = {"free_episode_count", "episode_count"}


def stage_after_resolution(kind: str) -> str:
    """剧目确定后进入的阶段：有逐剧参数的版权方先读详情，避免候选选择后跳过。"""

    return {"duiba": "duiba_preview", "gangganhao": "ggh_detail", "rongliang": "rl_detail"}.get(
        kind, "lookup"
    )
```

`_resolved_drama` 中 `.values(... **candidate)` 改为 `**{k: v for k, v in candidate.items() if k not in _DRAMA_FACTS}`，返回值保持 `{"id": str(row.id), **candidate}`（剧事实保留在 `work["drama"]`）。兑吧、刚刚好、容量 search 结束处的 `work["stage"] = "duiba_preview" | "ggh_detail" | "rl_detail"` 改为 `stage_after_resolution(kind)`；`repository.select_preparation_candidate` 中 `work.update(stage="lookup", ...)` 改为 `work.update(stage=stage_after_resolution(connection.kind), lookup_cursor=None, lookup_found=None)`。

- [ ] **Step 5: 兑吧 detail 阶段按剧卡点集**（`stage == "duiba_preview"`）

```python
        episode = _positive(config.get("episode", 1))
        # 卡点集未调整时与后台/CLI 一致取剧免费集数；剧单缺失时回退为起播集。
        card_point = _positive(
            config.get("card_point_episode")
            or work["drama"].get("free_episode_count")
            or episode
        )
        ...
        effective = dict(config)
        effective.update(card_point_episode=card_point, episode=episode)
        effective.setdefault("miniapp_id", application_id)
```

- [ ] **Step 6: 刚刚好 detail 阶段按剧免费集数与校验**（`stage == "ggh_detail"`）

```python
        drama = work["drama"]
        # 免费集数未调整时取剧自身设置，缺失时为 3，与 CLI 一致。
        free = config.get("free_episode_count")
        if free is None:
            free = drama.get("free_episode_count")
        free = 3 if free is None else int(free)
        seq = _positive(config.get("episode_seq", 1))
        total = drama.get("episode_count")
        # 校验与刚刚好后台一致；不满足时只让该剧冲突，不静默改值。
        if (
            free < 0
            or (total is not None and free > total)
            or (free == 0 and seq != 1)
            or seq > max(free, 1)
        ):
            raise _error("link_episode_out_of_range")
        effective = dict(config)
        effective.update(
            free_episode_count=free,
            episode_seq=seq,
            authorizer_app_id=application_id,
            series_id=detail["series_id"],
            series_title=detail["series_title"],
            delivery_mode=delivery_mode,
        )
```

`ERROR_MESSAGES` 增加 `"link_episode_out_of_range": "跳转集或免费集数超出该剧可选范围，请单独调整参数"`；`run_link_item` 异常映射中把 `config_conflict` 集合改为 `{"config_conflict", "config_unverifiable", "link_episode_out_of_range"}`；`_run_gangganhao_item` 内抛出的异常走同一映射（确认其调用链被 `run_link_item` 外层 `except` 捕获）。`ERROR_MESSAGES` 同时增加 `"link_config_invalid": "取链参数无效"` 与 `"link_config_template_required": "付费或混合变现应用需要支付模板"`。

- [ ] **Step 7: 唯一合并入口**

`repository.create_preparation_request` 在取得应用与连接后、计算 digest 前：

```python
    application = get_application(
        session, context=context, connection_id=connection_id, application_id=application_id
    )
    connection = get_connection(session, context=context, connection_id=connection_id)
    ...
    # 应用默认值与本次调整在此唯一入口合并；已合并结果再次合并保持不变。
    config = effective_link_config(connection.kind, application, config)
```

`drafts.py` 在计算 `provider_digest` 前计算一次：

```python
    connection = get_connection(session, context=context, connection_id=draft.provider_connection_id)
    application = get_application(
        session,
        context=context,
        connection_id=draft.provider_connection_id,
        application_id=draft.application_id,
    )
    # 与 create_preparation_request 使用同一合并规则，保证复用判断比较的是生效参数。
    link_config = effective_link_config(connection.kind, application, draft.link_config)
```

并把该函数中 `draft.link_config` 的三处使用（digest、`LinkPreparation.config ==` 比较、`prepare_links(config=...)`）改为 `link_config`。`DomainError` 原样抛出（错误码 `link_config_invalid` / `link_config_template_required`）。手动版权方（`kind == "other"`）不走此路径，保持现状。

- [ ] **Step 8: 运行测试**

Run: `cd backend && uv run pytest tests/modules/providers tests/modules/builds/test_drafts.py -q`
Expected: PASS；若既有用例断言 `LinkPreparation.config == {"episode": 1}` 之外的旧形态（例如带 `promote_name`），按新契约更新断言。

- [ ] **Step 9: 提交**

```bash
git add backend/app/modules/providers/schemas.py backend/app/modules/providers/repository.py backend/app/modules/providers/link_steps.py backend/app/modules/providers/adapters/duiba.py backend/app/modules/providers/adapters/gangganhao.py backend/app/modules/providers/adapters/contract.py backend/app/modules/builds/drafts.py backend/tests/modules/providers backend/tests/modules/builds/test_drafts.py
git commit -m "providers: 取链参数按应用默认与剧自身设置逐剧生效"
```

---

### Task 5: 容量剧集与别名对齐后台网页

**Files:**
- Modify: `backend/app/modules/providers/adapters/rongliang.py`（新增 `compilations_alias`）
- Modify: `backend/app/modules/providers/link_steps.py:1192-1215`（`rl_detail`）
- Test: `backend/tests/modules/providers/test_rongliang_protocol.py`、`test_new_provider_workflows.py`

**Interfaces:**
- Produces: `RongliangClient.compilations_alias(compilation_id: str) -> str`

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"aliasList": ["罪爱难逃", "备用"], "originalTitle": "Broken Altar"}, "罪爱难逃"),
        ({"aliasList": [], "originalTitle": "Broken Altar"}, "Broken Altar"),
        ({"aliasList": None}, ""),
    ],
)
def test_rongliang_alias_follows_web_form(payload, expected):
    def handle(request):
        assert request.url.path.endswith("/link/compilations_alias")
        assert request.url.params["compilationsId"] == "77"
        return httpx.Response(200, json={"code": 0, "data": payload})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http:
        client = RongliangClient(http, token="t")
        assert client.compilations_alias("77") == expected
```

（构造方式以 `test_rongliang_protocol.py` 现有写法为准。）工作流用例：剧集下拉返回 `[{"episodicDramaId": 501}, {"episodicDramaId": 502}]`、别名接口返回 `aliasList=["罪爱难逃"]`，断言建链体 `episodicDramaId == 501`、`compilationsAlias == "罪爱难逃"`，计划名 `planName` 写入 `PromotionLink.protected_base`。

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/modules/providers/test_rongliang_protocol.py tests/modules/providers/test_new_provider_workflows.py -k rongliang -q`
Expected: FAIL

- [ ] **Step 3: 实现**

`rongliang.py`：

```python
    def compilations_alias(self, compilation_id: str) -> str:
        """与后台新增链接表单一致：取第一个别名，没有别名用原名；别名决定计划名剧名部分。"""

        data = self._request(
            "GET", "/link/compilations_alias", query={"compilationsId": external_id(compilation_id)}
        )
        if not isinstance(data, dict):
            return ""
        aliases = data.get("aliasList")
        if isinstance(aliases, list) and aliases and isinstance(aliases[0], str):
            return aliases[0]
        title = data.get("originalTitle")
        return title if isinstance(title, str) else ""
```

`link_steps.py` 的 `rl_detail`：

```python
    if stage == "rl_detail":
        compilation_id = work["drama"]["external_drama_id"]
        options = client.episode_options(compilation_id)
        if not options:
            raise _error("provider_request_invalid")
        # 后台网页固定选剧集下拉第一项，不开放其他起播集；这里保持一致。
        episode_id = options[0].get("episodicDramaId")
        if episode_id is None:
            raise _error("provider_schema_unsupported")
        effective = dict(config)
        effective.update(
            client_id=application_id,
            episodic_drama_id=episode_id,
            platform=1,
            delivery_type=1,
            link_type=2,
            alias=client.compilations_alias(compilation_id),
        )
```

（`config` 对容量恒为 `{}`，Task 1 已拒绝任何用户键。）

- [ ] **Step 4: 运行测试**

Run: `cd backend && uv run pytest tests/modules/providers -k rongliang -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/modules/providers/adapters/rongliang.py backend/app/modules/providers/link_steps.py backend/tests/modules/providers/test_rongliang_protocol.py backend/tests/modules/providers/test_new_provider_workflows.py
git commit -m "rongliang: 剧集与别名按后台新增链接表单自动填写"
```

---

### Task 6: 网眼推广名称交给网眼生成，超时恢复按计数

**Files:**
- Modify: `backend/app/modules/providers/adapters/wangyan.py:345-361`
- Modify: `backend/app/modules/providers/wangyan_steps.py`
- Modify: `backend/tests/modules/providers/test_wangyan_workflow.py`、`test_wangyan_protocol.py`、`backend/tests/acceptance/scenario.py:455-480`

**Interfaces:**
- Produces: `work["created_effect"]: str`（本条目已发送过的建链 effect ID，取代 `work["promote_name"]` 作为恢复标记）

规则（与 spec 一致）：建链只发生在“完整扫描证明无同参数链接”之后，因此恢复扫描中看到的同参数链接都是这次建链之后出现的：恰 1 条→采用；0 条→该 effect 记为 `confirmed_absent` 并重新建链；≥2 条→`result_unknown` 人工核实；扫描不完整→`result_unknown`。

- [ ] **Step 1: 改写测试**

`test_wangyan_workflow.py`：
- `Remote.__call__` 中建链断言改为 `assert "promote_name" not in payload`，`remote_link(901)` 不再传 `name`。
- 删除依赖名字的用例：`test_unknown_create_exact_read_rechecks_correlated_name`、`test_direct_known_id_receipt_does_not_require_optional_remote_name`、`test_direct_known_id_still_rejects_a_nonempty_wrong_name`。
- 改写 `test_unknown_post_only_performs_correlated_reads`：

```python
@pytest.mark.parametrize("result", ["unique", "ambiguous"])
def test_unknown_post_adopts_single_new_link_or_stops(workflow, result):
    remote = workflow[-1]
    remote.lose_reply = True
    remote.duplicate_created = result == "ambiguous"
    item = finish(workflow, limit=12)
    assert len(writes(workflow)) == 1
    assert item.status == ("ready" if result == "unique" else "result_unknown"), item.resolved
    with Session(workflow[0]) as session:
        effect = session.exec(select(ProviderEffect).where(ProviderEffect.step == "create")).one()
        assert (effect.remote_id == "901") == (result == "unique")


def test_unknown_post_with_no_new_link_recreates_once(workflow):
    remote = workflow[-1]
    remote.lose_reply = True
    remote.expose_created = False

    def recover_network():
        # 第一次请求确实未落地；之后网络恢复，第二次建链正常返回。
        remote.lose_reply = False
        remote.expose_created = True
        remote.after_post = None

    remote.after_post = recover_network
    item = finish(workflow, limit=20)
    assert item.status == "ready", item.resolved
    assert len(writes(workflow)) == 2
    with Session(workflow[0]) as session:
        effect = session.exec(select(ProviderEffect).where(ProviderEffect.step == "create")).one()
        assert effect.status == "succeeded" and effect.remote_id == "901"


def test_unknown_post_with_incomplete_recovery_scan_never_recreates(workflow):
    remote = workflow[-1]
    remote.lose_reply = True
    remote.expose_created = False
    while not writes(workflow):
        advance(workflow)
    remote.rows = [remote_link(i, episode=2) for i in range(100, 121)]
    remote.rows[-1] = remote_link(100, episode=2)  # 分页重复，扫描不完整
    item = finish(workflow, limit=10)
    assert item.status == "result_unknown"
    assert len(writes(workflow)) == 1
```

注意 `recover_network` 在第一次 POST 中执行时 `lose_reply` 已被读取前置，故把 `Remote.__call__` 中 `if self.after_post: self.after_post()` 移到 `if self.lose_reply: raise` 判断**之后**会改变语义；改为在 `__call__` 开头缓存 `lose = self.lose_reply`，`after_post` 执行后用缓存值决定是否抛超时。

`test_wangyan_protocol.py`：line 248-256 的建链用例去掉 `promote_name` 字段与断言；line 377-380 的两个 `promote_name` 非法值用例改为断言 `create_step("create", {"vid": "opaque", "drama_num": 1, "promote_name": "x"})` 抛 `provider_request_invalid`。

`tests/acceptance/scenario.py:462-474`：删除 `startswith("ytf-")` 断言，回执中的 `promote_name` 改为固定 `"26017-A1180-TT-auto"`（模拟网眼生成）。

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && uv run pytest tests/modules/providers/test_wangyan_workflow.py tests/modules/providers/test_wangyan_protocol.py -q`
Expected: FAIL（请求体仍含 `promote_name`；0 条仍为 `result_unknown`）

- [ ] **Step 3: adapter 去掉 `promote_name`**

```python
    def create_step(self, step: str, payload: JsonDict) -> JsonDict:
        # 与 CLI 一致不传推广名称，由网眼后台自动生成。
        if step != "create" or set(payload) - {"vid", "drama_num"}:
            raise failure("provider_request_invalid")
```

并删除 `if "promote_name" in payload:` 整段。

- [ ] **Step 4: `wangyan_steps.py` 改造**

1. `_recovery_effect`：删除 `or work.get("promote_name") != "ytf-" + effect.id.hex` 条件。
2. `_create`：删除 `name = ...`、`work["promote_name"] = name` 及其校验；`payload` 去掉 `promote_name`；`if effect.status != "pending":` 改为 `if effect.status not in {"pending", "confirmed_absent"}:`；在 `claim_effect` 成功后写 `work["created_effect"] = str(identity)`。
3. `verified_data`：删除 `require_name` 参数及推广名称校验块；调用处同步删除实参与 `direct_receipt` 计算（`direct_receipt` 仅用于名称校验，随之删除）。
4. `advance` 的 verify 分支：`if work.get("promote_name") and not work.get("uncertain_effect"):` 改为 `if work.get("created_effect") and not work.get("uncertain_effect"):`。
5. 扫描循环：删除 `if recovering and row.get("promote_name") != work.get("promote_name"): continue`；扫描完成后：

```python
            scan["done"] = True
            if recovering and scan["matches"] == 0:
                # 建链前已完整确认无同参数链接，恢复扫描仍为 0 条说明本次未落地，可安全重建。
                _confirm_absent(session, context, work, config, item_id)
                work["lookup_complete"], work["stage"] = True, "create"
            elif recovering and scan["matches"] != 1:
                _checkpoint(session, context, item_id, token, work)
                raise core._error("provider_result_unknown")
            elif scan["candidate"]:
                work["remote_id"], work["stage"] = scan["candidate"], "verify"
            else:
                work["lookup_complete"], work["stage"] = True, "create"
```

新增：

```python
def _confirm_absent(
    session: Session,
    context: TenantContext,
    work: dict[str, Any],
    config: dict[str, Any],
    item_id: UUID,
) -> None:
    effect = _recovery_effect(session, context, item_id, work, config)
    effect.status, effect.remote_id = "confirmed_absent", None
    effect.result = {**effect.result, "recovered": True, "found": False}
    session.add(effect)
    work.pop("uncertain_effect", None)
    work.pop("uncertain_attempt_token", None)
```

（`_recovery_effect` 需在同一事务内带行锁读取：把其内部 `_creation` 返回的对象改为 `session.exec(select(ProviderEffect).where(ProviderEffect.id == effect.id).with_for_update().execution_options(populate_existing=True)).one()` 后再校验。）

- [ ] **Step 5: 运行测试**

Run: `cd backend && uv run pytest tests/modules/providers/test_wangyan_workflow.py tests/modules/providers/test_wangyan_protocol.py tests/modules/providers/test_link_recovery.py tests/modules/providers/test_link_concurrency.py -q`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add backend/app/modules/providers/adapters/wangyan.py backend/app/modules/providers/wangyan_steps.py backend/tests/modules/providers/test_wangyan_workflow.py backend/tests/modules/providers/test_wangyan_protocol.py backend/tests/acceptance/scenario.py
git commit -m "wangyan: 推广名称交由网眼生成，超时后按同参数链接计数恢复"
```

---

### Task 7: 前端——应用默认值与搭建页摘要

**Files:**
- Regenerate: `frontend/openapi.json`、`frontend/src/client/**`（`bash scripts/generate-client.sh`）
- Modify: `frontend/src/features/builds/ProviderLinkConfigFields.tsx`（整文件重写）
- Create: `frontend/src/features/builds/LinkConfigSummary.tsx`
- Create: `frontend/src/features/providers/ApplicationLinkDefaults.tsx`
- Modify: `frontend/src/features/providers/ConnectionPanel.tsx:574-660`（应用表增加操作列）
- Modify: `frontend/src/features/builds/BuildInputPage.tsx:118-142, 561-576`
- Modify: `frontend/tests/utils/buildsBoundary.ts:351-362`
- Test: `frontend/tests/build-link-config.spec.ts`（新建）、`frontend/tests/providers.spec.ts`

**Interfaces:**
- Consumes: `LinkConfigSchema`（`fields[].source/rule/options[{value,label}]`、`defaults`、`application_defaults`）、`ProvidersService.patchApplicationLinkDefaults`（生成器按 `operation_id` 命名，执行者以生成结果为准）
- Produces:
  - `ProviderLinkConfigFields({ schema, value, mode: "defaults" | "overrides", disabled, onChange })`
  - `LinkConfigSummary({ schema, overrides })`
  - `ApplicationLinkDefaults({ tenantId, connection, application })`

- [ ] **Step 1: 重新生成客户端**

Run: `bash scripts/generate-client.sh`
Expected: `frontend/src/client` 中 `LinkConfigField` 含 `source`、`rule`、`minimum`，`ProviderApplicationPublic` 含 `link_defaults`。

- [ ] **Step 2: 写失败的 Playwright 用例** `frontend/tests/build-link-config.spec.ts`

```ts
import { expect, test } from "@playwright/test"
import { BC, buildsBoundary, pickInputs, T } from "./utils/buildsBoundary"

test("取链参数默认只读展示来源，调整后只提交填写的字段", async ({ page }) => {
  const api = await buildsBoundary(page, { linkKind: "duiba" })
  await page.goto(`/tenants/${T}/builds/new?bc_id=${BC}`)
  await pickInputs(page)
  const card = page.getByRole("region", { name: "自动取链参数" })
  await expect(card.getByText("起播集 2（应用默认）")).toBeVisible()
  await expect(card.getByText("卡点集：按剧免费集数")).toBeVisible()
  await expect(card.getByLabel("卡点集")).toHaveCount(0)
  await card.getByRole("button", { name: "本次单独调整" }).click()
  await card.getByLabel("卡点集").fill("5")
  await card.getByLabel("起播集").fill("")
  await page.getByRole("button", { name: "保存草稿", exact: true }).click()
  await expect
    .poll(() => api.requests.find((r) => r.method === "POST" && r.path.endsWith("/build-drafts"))?.body.link_config)
    .toEqual({ card_point_episode: 5 })
})

test("容量无需设置取链参数", async ({ page }) => {
  await buildsBoundary(page, { linkKind: "rongliang" })
  await page.goto(`/tenants/${T}/builds/new?bc_id=${BC}`)
  await pickInputs(page)
  await expect(page.getByText("参数与版权方后台一致，无需设置")).toBeVisible()
  await expect(page.getByRole("button", { name: "本次单独调整" })).toHaveCount(0)
})

test("刚刚好付费应用未设置支付模板时提示去连接设置", async ({ page }) => {
  await buildsBoundary(page, { linkKind: "gangganhao-paid-no-template" })
  await page.goto(`/tenants/${T}/builds/new?bc_id=${BC}`)
  await pickInputs(page)
  await expect(page.getByText("付费或混合变现应用需要支付模板，请在版权方连接的推广应用中设置")).toBeVisible()
})
```

`buildsBoundary.ts`：`options` 增加 `linkKind?: "wangyan" | "duiba" | "rongliang" | "gangganhao-paid-no-template"`（默认 `"wangyan"`），capabilities 分支按它返回：

```ts
const linkSchemas: Record<string, LinkConfigSchema> = {
  wangyan: {
    provider_kind: "wangyan",
    fields: [{ name: "episode", label: "起播集", type: "integer", default: 1, source: "application_default", minimum: 1, required: false, options: [] }],
    defaults: { episode: 1 },
    application_defaults: {},
    schema_version: 2,
  },
  duiba: {
    provider_kind: "duiba",
    fields: [
      { name: "episode", label: "起播集", type: "integer", default: 1, source: "application_default", minimum: 1, required: false, options: [] },
      { name: "card_point_episode", label: "卡点集", type: "integer", source: "per_drama", rule: "按剧免费集数", minimum: 1, required: false, options: [] },
    ],
    defaults: { episode: 1 },
    application_defaults: { episode: 2 },
    schema_version: 2,
  },
  rongliang: { provider_kind: "rongliang", fields: [], defaults: {}, application_defaults: {}, schema_version: 2 },
  "gangganhao-paid-no-template": {
    provider_kind: "gangganhao",
    fields: [
      { name: "free_episode_count", label: "免费集数", type: "integer", source: "per_drama", rule: "按剧免费集数，缺失时为 3", minimum: 0, required: false, options: [] },
      { name: "episode_seq", label: "跳转集", type: "integer", default: 1, source: "application_default", minimum: 1, required: false, options: [] },
      { name: "payment_template_id", label: "支付模板", type: "select", source: "application_default", minimum: 1, required: true, options: [{ value: "7", label: "周卡" }] },
    ],
    defaults: { episode_seq: 1 },
    application_defaults: {},
    schema_version: 2,
  },
}
```

`providers.spec.ts` 新增用例：在连接详情的「已发现应用」表点击「取链默认值」，填写起播集 `3` 保存，断言 PATCH 请求体为 `{ link_defaults: { episode: 3 } }`，对话框关闭且列表刷新；在该文件 boundary 中为 `PATCH .../link-defaults` 返回更新后的 schema、为 `.../capabilities` 返回 wangyan schema。

- [ ] **Step 3: 运行确认失败**

Run: `cd frontend && bunx playwright test tests/build-link-config.spec.ts tests/providers.spec.ts`
Expected: FAIL

- [ ] **Step 4: 重写 `ProviderLinkConfigFields.tsx`**

```tsx
import type { LinkConfigSchema } from "@/client"
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

export type LinkConfigValues = Record<string, number>

// defaults：推广应用默认值，只显示可设默认的字段；overrides：本次单独调整，留空即沿用下层值。
export function ProviderLinkConfigFields({
  schema,
  value,
  mode,
  disabled,
  onChange,
}: {
  schema: LinkConfigSchema
  value: LinkConfigValues
  mode: "defaults" | "overrides"
  disabled: boolean
  onChange: (next: LinkConfigValues) => void
}) {
  const fields = schema.fields.filter(
    (field) => mode === "overrides" || field.source === "application_default",
  )
  if (!fields.length) return null
  const update = (name: string, raw: string) => {
    const next = { ...value }
    // 清空即删除该键，不提交 null，后端按“未调整”处理。
    if (raw === "") delete next[name]
    else next[name] = Number(raw)
    onChange(next)
  }
  return (
    <FieldGroup className="grid min-w-0 gap-4 md:grid-cols-2">
      {fields.map((field) => {
        const current = value[field.name]
        const placeholder =
          field.source === "per_drama"
            ? (field.rule ?? "")
            : String(schema.application_defaults?.[field.name] ?? schema.defaults?.[field.name] ?? "")
        return (
          <Field key={field.name}>
            <FieldLabel htmlFor={`link-config-${field.name}`}>{field.label}</FieldLabel>
            {field.type === "select" ? (
              <Select
                value={current === undefined ? "" : String(current)}
                disabled={disabled || !field.options?.length}
                onValueChange={(raw) => update(field.name, raw)}
              >
                <SelectTrigger id={`link-config-${field.name}`} aria-label={field.label}>
                  <SelectValue placeholder={field.options?.length ? "请选择" : "暂无可选项，请重新验证连接"} />
                </SelectTrigger>
                <SelectContent>
                  {field.options?.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : (
              <Input
                id={`link-config-${field.name}`}
                type="number"
                min={field.minimum ?? 1}
                value={current === undefined ? "" : String(current)}
                placeholder={placeholder}
                disabled={disabled}
                onChange={(event) => update(field.name, event.currentTarget.value)}
              />
            )}
            {field.rule && mode === "overrides" && (
              <FieldDescription>留空则{field.rule}</FieldDescription>
            )}
          </Field>
        )
      })}
    </FieldGroup>
  )
}
```

- [ ] **Step 5: 新建 `LinkConfigSummary.tsx`**

```tsx
import type { LinkConfigSchema } from "@/client"

const optionLabel = (schema: LinkConfigSchema, name: string, value: unknown) =>
  schema.fields
    .find((field) => field.name === name)
    ?.options?.find((option) => option.value === String(value))?.label ?? String(value)

// 按“本次调整 > 应用默认 > 按剧自动 / 系统默认”展示每个参数实际生效的值与来源。
export function LinkConfigSummary({
  schema,
  overrides,
}: {
  schema: LinkConfigSchema
  overrides: Record<string, number>
}) {
  if (!schema.fields.length)
    return <p className="text-sm text-muted-foreground">参数与版权方后台一致，无需设置</p>
  return (
    <ul className="flex flex-col gap-1 text-sm">
      {schema.fields.map((field) => {
        const override = overrides[field.name]
        const appDefault = schema.application_defaults?.[field.name]
        let text: string
        if (override !== undefined) text = `${field.label} ${optionLabel(schema, field.name, override)}（本次调整）`
        else if (appDefault !== undefined && appDefault !== null)
          text = `${field.label} ${optionLabel(schema, field.name, appDefault)}（应用默认）`
        else if (field.source === "per_drama") text = `${field.label}：${field.rule}`
        else if (schema.defaults?.[field.name] !== undefined)
          text = `${field.label} ${String(schema.defaults[field.name])}（系统默认）`
        else text = `${field.label}：未设置`
        return <li key={field.name}>{text}</li>
      })}
    </ul>
  )
}
```

- [ ] **Step 6: 搭建页接入**（`BuildInputPage.tsx`）

1. 删除 118-142 行中把 `capabilities.data.defaults` 合并进 `linkConfig` 的 `useEffect`（不再把默认值写进草稿）。`Values.linkConfig` 类型改为 `Record<string, number>`。
2. 新增 `const [adjusting, setAdjusting] = useState(() => Object.keys(values.linkConfig).length > 0)`。
3. 参数卡片替换为：

```tsx
      {values.connection !== "other" && capabilities.data && (
        <Card className="min-w-0" role="region" aria-label="自动取链参数">
          <CardHeader>
            <CardTitle>自动取链参数</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <LinkConfigSummary schema={capabilities.data} overrides={values.linkConfig} />
            {templateMissing(capabilities.data, values.linkConfig) && (
              <Alert variant="destructive">
                <AlertDescription>
                  付费或混合变现应用需要支付模板，请在版权方连接的推广应用中设置
                </AlertDescription>
              </Alert>
            )}
            {capabilities.data.fields.length > 0 && (
              <Button
                type="button"
                variant="outline"
                className="self-start"
                disabled={disabled}
                onClick={() => setAdjusting((open) => !open)}
              >
                本次单独调整
              </Button>
            )}
            {adjusting && (
              <ProviderLinkConfigFields
                schema={capabilities.data}
                value={values.linkConfig}
                mode="overrides"
                disabled={disabled}
                onChange={(linkConfig) => change({ linkConfig })}
              />
            )}
          </CardContent>
        </Card>
      )}
```

并在文件内定义：

```tsx
// 付费应用的支付模板既无本次调整也无应用默认时，提交前提示，避免取链阶段才失败。
const templateMissing = (schema: LinkConfigSchema, overrides: Record<string, number>) =>
  schema.fields.some(
    (field) =>
      field.name === "payment_template_id" &&
      field.required &&
      overrides[field.name] === undefined &&
      schema.application_defaults?.[field.name] === undefined,
  )
```

提交按钮的禁用条件追加 `|| (capabilities.data ? templateMissing(capabilities.data, values.linkConfig) : false)`。切换版权方/应用时已有的 `linkConfig: {}` 重置保留。

- [ ] **Step 7: 新建 `ApplicationLinkDefaults.tsx` 并接入应用表**

```tsx
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import type { ProviderApplicationPublic, ProviderConnectionPublic } from "@/client"
import { ProvidersService } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { ProviderLinkConfigFields } from "@/features/builds/ProviderLinkConfigFields"
import { capabilitiesQuery, providerKey } from "./queries"

// 推广应用的取链默认值：每次搭建自动带入，搭建页可单次调整覆盖。
export function ApplicationLinkDefaults({
  tenantId,
  connection,
  application,
}: {
  tenantId: string
  connection: ProviderConnectionPublic
  application: ProviderApplicationPublic
}) {
  const [open, setOpen] = useState(false)
  const [value, setValue] = useState<Record<string, number>>({})
  const client = useQueryClient()
  const schema = useQuery({
    ...capabilitiesQuery(tenantId, connection.id, application.external_id),
    enabled: open,
  })
  const save = useMutation({
    mutationFn: async () =>
      (
        await ProvidersService.patchApplicationLinkDefaults({
          path: {
            tenant_id: tenantId,
            connection_id: connection.id,
            application_id: application.external_id,
          },
          body: { link_defaults: value },
          throwOnError: true,
        })
      ).data,
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: providerKey(tenantId) })
      setOpen(false)
    },
  })
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next)
        if (next) setValue({ ...(application.link_defaults as Record<string, number>) })
      }}
    >
      <DialogTrigger asChild>
        <Button variant="outline" disabled={!application.available}>
          取链默认值
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{application.name} · 取链默认值</DialogTitle>
        </DialogHeader>
        {schema.data && (
          schema.data.fields.some((field) => field.source === "application_default") ? (
            <ProviderLinkConfigFields
              schema={schema.data}
              value={value}
              mode="defaults"
              disabled={save.isPending}
              onChange={setValue}
            />
          ) : (
            <p className="text-sm text-muted-foreground">参数与版权方后台一致，无需设置</p>
          )
        )}
        {save.error && <p className="text-sm text-destructive">保存失败，请检查参数后重试</p>}
        <DialogFooter>
          <Button onClick={() => save.mutate()} disabled={save.isPending || !schema.data}>
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
```

`ConnectionPanel.tsx` 的 `Applications` 在 `onSelect` 为空（连接管理视图）时追加一列：

```tsx
      ...(!onSelect
        ? [
            {
              header: "取链默认值",
              cell: ({ row }: { row: { original: ProviderApplicationPublic } }) => (
                <ApplicationLinkDefaults tenantId={tenantId} connection={connection} application={row.original} />
              ),
            },
          ]
        : []),
```

（`useMemo` 依赖补 `tenantId`、`connection`。只读角色由后端 403 拒绝；若该文件已有基于角色隐藏管理按钮的写法，沿用同一判断隐藏此列。）

- [ ] **Step 8: 运行前端检查**

Run: `cd frontend && bun run lint && bun run build && bunx playwright test tests/build-link-config.spec.ts tests/providers.spec.ts tests/build-channels.spec.ts tests/build-preparation.spec.ts`
Expected: 全部 PASS

- [ ] **Step 9: 提交**

```bash
git add frontend/openapi.json frontend/src/client frontend/src/features/builds/ProviderLinkConfigFields.tsx frontend/src/features/builds/LinkConfigSummary.tsx frontend/src/features/builds/BuildInputPage.tsx frontend/src/features/providers/ApplicationLinkDefaults.tsx frontend/src/features/providers/ConnectionPanel.tsx frontend/tests/build-link-config.spec.ts frontend/tests/providers.spec.ts frontend/tests/utils/buildsBoundary.ts
git commit -m "frontend: 推广应用设置取链默认值，搭建页只读展示并支持本次调整"
```

---

### Task 8: 全量验证与进度记录

**Files:**
- Modify: `docs/implementation-progress.md`

- [ ] **Step 1: 后端全量**

Run: `cd backend && bash scripts/lint.sh && bash scripts/test.sh`
Expected: lint 无报错，测试全部通过。

- [ ] **Step 2: 前端全量**

Run: `cd frontend && bun run lint && bun run build && bunx playwright test`
Expected: 全部通过。

- [ ] **Step 3: 记录进度**

在 `docs/implementation-progress.md` 追加 2026-10-11 条目：完成的 Task 1-7 及提交号、迁移 `0021_provider_link_defaults`、行为变化（网眼推广名称由网眼生成、网眼 0 条恢复会重建、容量剧集固定第一项与别名自动取、兑吧卡点集/刚刚好免费集数按剧默认、刚刚好支付模板随连接验证刷新、候选选择进入 detail 阶段的缺陷修复）、验证命令与结果。标注“未在真实版权方后台联调”。

- [ ] **Step 4: 提交**

```bash
git add docs/implementation-progress.md
git commit -m "docs: 记录取链参数对齐的实现与验证"
```
