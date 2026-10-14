"""自动规则持久化:规则、每次运行、命中对象、租户设置、飞书配置。"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel

from app.modules.ads.models import AwareDateTime


def _now() -> datetime:
    return datetime.now(UTC)


class AutomationRule(SQLModel, table=True):
    __tablename__ = "automation_rule"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_automation_rule_name"),
        Index("ix_automation_rule_due", "enabled", "next_run_at"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    name: str = Field(max_length=100)
    enabled: bool = True
    version: int = 1
    definition: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    created_by: UUID = Field(foreign_key="user.id")
    updated_by: UUID = Field(foreign_key="user.id")
    created_at: datetime = Field(default_factory=_now, sa_type=AwareDateTime)
    updated_at: datetime = Field(default_factory=_now, sa_type=AwareDateTime)
    next_run_at: datetime | None = Field(default=None, sa_type=AwareDateTime)
    last_run_at: datetime | None = Field(default=None, sa_type=AwareDateTime)


class AutomationRuleRun(SQLModel, table=True):
    __tablename__ = "automation_rule_run"
    __table_args__ = (
        UniqueConstraint("rule_id", "slot_at", name="uq_automation_run_slot"),
        Index("ix_automation_run_tenant_time", "tenant_id", "started_at"),
        CheckConstraint(
            "status IN ('RUNNING','COMPLETE','FAILED')", name="ck_automation_run_status"
        ),
        CheckConstraint(
            "notify_status IN ('NOT_REQUIRED','PENDING','SENT','FAILED')",
            name="ck_automation_run_notify",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    rule_id: UUID = Field(foreign_key="automation_rule.id", ondelete="CASCADE")
    rule_version: int
    # 运行所用的规则定义快照;规则后续修改不影响历史记录解释。
    definition: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    slot_at: datetime = Field(sa_type=AwareDateTime)
    status: str = Field(default="RUNNING", max_length=16)
    started_at: datetime = Field(default_factory=_now, sa_type=AwareDateTime)
    finished_at: datetime | None = Field(default=None, sa_type=AwareDateTime)
    checked_count: int = 0
    hit_count: int = 0
    skipped_count: int = 0
    skipped_accounts: list[dict[str, str]] = Field(
        default_factory=list, sa_column=Column(JSONB, nullable=False)
    )
    error_code: str | None = Field(default=None, max_length=64)
    notify_status: str = Field(default="NOT_REQUIRED", max_length=16)
    notify_error: str | None = Field(default=None, max_length=500)
    mode: str = Field(default="NOTIFY", max_length=16)
    # NOT_REQUIRED / AWAITING_CONFIRMATION / QUEUED / SUBMITTED / PARTIAL / FAILED / DISMISSED / EXPIRED
    execution_status: str = Field(default="NOT_REQUIRED", max_length=24)
    execution_summary: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSONB, nullable=False)
    )


class AutomationRuleHit(SQLModel, table=True):
    __tablename__ = "automation_rule_hit"
    __table_args__ = (Index("ix_automation_hit_run", "run_id", "sequence"),)
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    run_id: UUID = Field(foreign_key="automation_rule_run.id", ondelete="CASCADE")
    sequence: int
    subject_key: str = Field(max_length=300)
    subject_label: str = Field(max_length=500)
    drama_name: str | None = Field(default=None, max_length=300)
    advertiser_id: str | None = Field(default=None, max_length=128)
    bc_id: str | None = Field(default=None, max_length=128)
    currency: str | None = Field(default=None, max_length=16)
    metrics: dict[str, Any] = Field(sa_column=Column(JSONB, nullable=False))
    targets: list[dict[str, Any]] = Field(sa_column=Column(JSONB, nullable=False))
    # targets 最多保存 max_targets 个；这里记录真实执行对象总数。
    target_count: int = 0
    outcome: str = Field(default="NOTIFIED", max_length=32)


class AutomationTenantSetting(SQLModel, table=True):
    __tablename__ = "automation_tenant_setting"
    tenant_id: UUID = Field(foreign_key="tenant.id", primary_key=True)
    # 自定义运行时段按租户时区解释;各账户指标仍按各自账户时区取日期。
    schedule_timezone: str = Field(default="Asia/Shanghai", max_length=64)
    paused: bool = False
    # 同一对象被一条规则改过后，冷却期内其他规则不得做相反动作。
    reverse_cooldown_hours: int = 24
    # 提高预算 / ROAS 时，同一对象当日累计涨幅上限（百分比）。
    daily_increase_limit_pct: int = 100
    updated_at: datetime = Field(default_factory=_now, sa_type=AwareDateTime)


class TenantFeishuSetting(SQLModel, table=True):
    __tablename__ = "tenant_feishu_setting"
    tenant_id: UUID = Field(foreign_key="tenant.id", primary_key=True)
    # 加密信封内为 {"url": Webhook, "secret": 签名密钥(可空串)}。
    ciphertext: str
    url_hint: str = Field(max_length=120)
    has_secret: bool = False
    enabled: bool = True
    updated_by: UUID = Field(foreign_key="user.id")
    updated_at: datetime = Field(default_factory=_now, sa_type=AwareDateTime)


class AutomationActionLedger(SQLModel, table=True):
    """每次提交给广告管理的规则动作；频控、冷却期与每日涨幅上限都以它为准。

    台账独立于运行与规则保留：运行清理或规则删除后，频控（如「仅一次」）与
    其他规则的冷却期冲突判断仍要看到这些动作。
    """

    __tablename__ = "automation_action_ledger"
    __table_args__ = (
        Index("ix_automation_ledger_target", "tenant_id", "target_key", "created_at"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    rule_id: UUID | None = Field(
        default=None, foreign_key="automation_rule.id", ondelete="SET NULL"
    )
    run_id: UUID | None = Field(
        default=None, foreign_key="automation_rule_run.id", ondelete="SET NULL"
    )
    target_key: str = Field(max_length=300)
    action: str = Field(max_length=32)
    original_value: str | None = Field(default=None, max_length=128)
    final_value: str | None = Field(default=None, max_length=128)
    management_task_id: UUID | None = None
    created_at: datetime = Field(default_factory=_now, sa_type=AwareDateTime)


class AutomationConfirmation(SQLModel, table=True):
    """确认后执行（或超出单次上限）的待确认批次，一次运行至多一个。"""

    __tablename__ = "automation_confirmation"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_automation_confirmation_run"),
        Index("ix_automation_confirmation_status", "tenant_id", "status", "created_at"),
        CheckConstraint(
            "status IN ('PENDING','CONFIRMED','DISMISSED','EXPIRED')",
            name="ck_automation_confirmation_status",
        ),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    tenant_id: UUID = Field(foreign_key="tenant.id")
    rule_id: UUID = Field(foreign_key="automation_rule.id", ondelete="CASCADE")
    run_id: UUID = Field(foreign_key="automation_rule_run.id", ondelete="CASCADE")
    status: str = Field(default="PENDING", max_length=16)
    decided_by: UUID | None = Field(default=None, foreign_key="user.id")
    decided_at: datetime | None = Field(default=None, sa_type=AwareDateTime)
    created_at: datetime = Field(default_factory=_now, sa_type=AwareDateTime)
