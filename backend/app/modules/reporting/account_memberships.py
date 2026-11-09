"""共享不可变账户成员版本；授权由调用方现有快照范围校验提供。"""

from collections.abc import Collection, Iterable, Sequence
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import text
from sqlmodel import Session, col, select

from app.modules.reporting.read_models.models import AccountMembershipVersion

if TYPE_CHECKING:
    from app.modules.reporting.query_models import QuerySnapshot, QuerySnapshotRow


def load_account_versions(
    session: Session,
    *,
    tenant_id: UUID,
    advertisers: Collection[str],
    version_ids: Collection[UUID],
) -> dict[UUID, AccountMembershipVersion]:
    wanted = set(version_ids)
    if not wanted:
        return {}
    versions = session.exec(
        select(AccountMembershipVersion).where(
            AccountMembershipVersion.tenant_id == tenant_id,
            col(AccountMembershipVersion.advertiser_id).in_(advertisers),
            col(AccountMembershipVersion.id).in_(wanted),
        )
    ).all()
    result = {version.id: version for version in versions}
    if set(result) != wanted:
        raise HTTPException(409, detail="query_membership_unavailable")
    return result


def snapshot_account_versions(
    session: Session, *, snapshot: QuerySnapshot, rows: Sequence[QuerySnapshotRow]
) -> dict[str, AccountMembershipVersion]:
    account_rows = [row for row in rows if row.dimension == "account"]
    if any(row.account_membership_version_id is None for row in account_rows):
        raise HTTPException(409, detail="query_membership_unavailable")
    versions = load_account_versions(
        session,
        tenant_id=snapshot.tenant_id,
        advertisers=snapshot.advertiser_ids,
        version_ids=[
            row.account_membership_version_id
            for row in account_rows
            if row.account_membership_version_id
        ],
    )
    result = {}
    for row in account_rows:
        assert row.account_membership_version_id is not None
        version = versions[row.account_membership_version_id]
        expected_key = f"{version.advertiser_id}:account:{version.advertiser_id}"
        if row.row_key != expected_key:
            raise HTTPException(409, detail="query_membership_unavailable")
        result[row.row_key] = version
    return result


def expanded_naming_versions(
    *, snapshot: QuerySnapshot, versions: Iterable[AccountMembershipVersion]
) -> dict[str, int]:
    if snapshot.filters.get("dimension") != "account":
        return dict(snapshot.naming_versions)
    names = {"directory": 1}
    for version in versions:
        names.update(version.naming_versions)
    return names


def prune_account_memberships(session: Session, *, limit: int = 100) -> None:
    # 退休宽限覆盖45秒构建租约，防止RR刚读到旧指针、尚未写入FK时被GC删除。
    # 存活快照及当前指针仍是硬引用，导出/已冻结选择已复制完整证据，不依赖短期版本。
    session.connection().execute(
        text("""DELETE FROM account_membership_version WHERE id IN (
            SELECT v.id FROM account_membership_version v
            WHERE v.retired_at < clock_timestamp()-interval '1 hour'
              AND NOT EXISTS(SELECT 1 FROM account_membership_current h WHERE h.version_id=v.id)
              AND NOT EXISTS(SELECT 1 FROM query_snapshot_row q WHERE q.account_membership_version_id=v.id)
            ORDER BY v.retired_at,v.id LIMIT :limit FOR UPDATE SKIP LOCKED
        )"""),
        {"limit": limit},
    )
