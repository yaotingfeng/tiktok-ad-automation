"""六维报表共享冻结身份解析；查询时保存紧凑键/版本，操作时展开原证据。"""

import json
from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import text
from sqlmodel import Session, col, select

from app.modules.reporting.account_memberships import load_account_versions
from app.modules.reporting.read_models.evidence import ReportEvidenceVersion
from app.modules.reporting.row_set import SnapshotMember

if TYPE_CHECKING:
    from app.modules.reporting.query_models import QuerySnapshot, QuerySnapshotRow


def _unavailable() -> HTTPException:
    return HTTPException(409, detail="query_membership_unavailable")


def _scalar_ref(
    tenant_id: UUID, advertisers: Collection[str], dimension: str, key: str
) -> dict[str, Any]:
    advertiser, found, remote = key.partition(f":{dimension}:")
    if not found or not remote or advertiser not in advertisers:
        raise _unavailable()
    return {
        "tenant_id": str(tenant_id),
        "advertiser_id": advertiser,
        "kind": dimension,
        "remote_id": remote,
    }


def _check_ref(ref: Mapping[str, Any], tenant_id: UUID, advertiser: str) -> None:
    try:
        valid = (
            UUID(str(ref["tenant_id"])) == tenant_id
            and ref["advertiser_id"] == advertiser
            and ref["kind"] == "ad"
            and isinstance(ref["remote_id"], str)
            and bool(ref["remote_id"])
        )
    except ValueError, TypeError, KeyError:
        valid = False
    if not valid:
        raise _unavailable()


def _material_key(key: str, advertisers: Collection[str]) -> list[str]:
    try:
        identity = json.loads(key)
    except ValueError, TypeError:
        raise _unavailable() from None
    if not isinstance(identity, list) or not all(
        isinstance(part, str) and part for part in identity
    ):
        raise _unavailable()
    if len(identity) == 2 and identity[0] == "material":
        return identity
    if len(identity) == 3 and identity[0] == "post" and identity[1] in advertisers:
        return identity
    raise _unavailable()


def members_from_data(
    session: Session,
    *,
    tenant_id: UUID,
    advertisers: Collection[str],
    dimension: str,
    rows: Sequence[Mapping[str, Any]],
) -> tuple[SnapshotMember, ...]:
    scope = set(advertisers)
    if dimension not in {"account", "campaign", "adgroup", "ad", "drama", "material"}:
        raise _unavailable()
    accounts = (
        load_account_versions(
            session,
            tenant_id=tenant_id,
            advertisers=scope,
            version_ids=[r["account_membership_version_id"] for r in rows],
        )
        if dimension == "account"
        else {}
    )
    wanted = {version for r in rows for version in r.get("evidence_versions", ())}
    evidence = {}
    if wanted:
        evidence = {
            v.id: v
            for v in session.exec(
                select(ReportEvidenceVersion).where(
                    ReportEvidenceVersion.tenant_id == tenant_id,
                    col(ReportEvidenceVersion.advertiser_id).in_(scope),
                    col(ReportEvidenceVersion.id).in_(wanted),
                )
            ).all()
        }
        if set(evidence) != wanted:
            raise _unavailable()
    result = []
    for row in rows:
        key = row["row_key"]
        refs: list[dict[str, Any]] = []
        uses: list[dict[str, Any]] = []
        if dimension == "account":
            account_version = accounts[row["account_membership_version_id"]]
            if (
                key
                != f"{account_version.advertiser_id}:account:{account_version.advertiser_id}"
            ):
                raise _unavailable()
            refs = account_version.refs
        elif dimension in {"campaign", "adgroup", "ad"}:
            refs = [_scalar_ref(tenant_id, scope, dimension, key)]
        elif dimension == "drama":
            for pair in row["member_keys"]:
                if (
                    not isinstance(pair, (list, tuple))
                    or len(pair) != 2
                    or pair[0] not in scope
                    or not isinstance(pair[1], str)
                    or not pair[1]
                ):
                    raise _unavailable()
                refs.append(
                    {
                        "tenant_id": str(tenant_id),
                        "advertiser_id": pair[0],
                        "kind": "campaign",
                        "remote_id": pair[1],
                    }
                )
        else:
            material_key = _material_key(key, scope)
            for identity in set(row.get("evidence_versions", ())):
                evidence_version = evidence[identity]
                if evidence_version.kind not in {
                    "material_account",
                    "material_ad",
                    "material_snapshot",
                }:
                    raise _unavailable()
                if (
                    material_key[0] == "post"
                    and evidence_version.advertiser_id != material_key[1]
                ):
                    raise _unavailable()
                for ref in evidence_version.refs:
                    _check_ref(ref, tenant_id, evidence_version.advertiser_id)
                    refs.append(ref)
                for usage in evidence_version.material_uses:
                    if not isinstance(usage, dict) or not isinstance(
                        usage.get("ad_ref"), dict
                    ):
                        raise _unavailable()
                    _check_ref(
                        usage["ad_ref"], tenant_id, evidence_version.advertiser_id
                    )
                    if (
                        usage.get("material_type") != "VIDEO"
                        or not isinstance(usage.get("platform_material_id"), str)
                        or not usage["platform_material_id"]
                    ):
                        raise _unavailable()
                    if usage.get("ad_material_id") is not None and not isinstance(
                        usage["ad_material_id"], str
                    ):
                        raise _unavailable()
                    # FK保证版本存在；仍需验证版本属于本行VID，不能把同租户另一素材混入操作。
                    if (
                        material_key[0] == "material"
                        and usage["platform_material_id"] != material_key[1]
                    ):
                        raise _unavailable()
                    uses.append(usage)
            refs = list(
                {
                    (r["advertiser_id"], r["kind"], r["remote_id"]): r for r in refs
                }.values()
            )
            refs.sort(key=lambda r: (r["advertiser_id"], r["remote_id"]))
            uses = list(
                {
                    (
                        u["ad_ref"]["advertiser_id"],
                        u["ad_ref"]["remote_id"],
                        u["platform_material_id"],
                        u.get("ad_material_id"),
                        u["material_type"],
                    ): u
                    for u in uses
                }.values()
            )
            uses.sort(
                key=lambda u: (
                    u["ad_ref"]["advertiser_id"],
                    u["ad_ref"]["remote_id"],
                    u["platform_material_id"],
                    u.get("ad_material_id") or "",
                    u["material_type"],
                )
            )
            if row.get("member_count", len(uses)) != len(uses):
                raise _unavailable()
        result.append(SnapshotMember(key, tuple(refs), tuple(uses)))
    return tuple(result)


def snapshot_members(
    session: Session, *, snapshot: QuerySnapshot, rows: Sequence[QuerySnapshotRow]
) -> dict[str, SnapshotMember]:
    if any(
        row.snapshot_id != snapshot.id or row.dimension != snapshot.filters["dimension"]
        for row in rows
    ):
        raise _unavailable()
    links: dict[str, list[UUID]] = defaultdict(list)
    if any(row.dimension == "material" for row in rows):
        for key, version in session.connection().execute(
            text("""SELECT row_key,version_id FROM query_snapshot_evidence
            WHERE snapshot_id=:snapshot AND row_key=ANY(:keys)"""),
            {"snapshot": snapshot.id, "keys": [r.row_key for r in rows]},
        ):
            links[key].append(version)
    descriptors = [
        {
            "row_key": r.row_key,
            "account_membership_version_id": r.account_membership_version_id,
            "member_keys": r.member_keys,
            "member_count": r.member_count,
            "evidence_versions": links[r.row_key],
        }
        for r in rows
    ]
    resolved = members_from_data(
        session,
        tenant_id=snapshot.tenant_id,
        advertisers=snapshot.advertiser_ids,
        dimension=snapshot.filters["dimension"],
        rows=descriptors,
    )
    return {r.row_key: r for r in resolved}


def pin_snapshot_names(session: Session, *, snapshot: QuerySnapshot) -> dict[str, int]:
    if snapshot.filters.get("dimension") not in {"campaign", "drama"}:
        return {"directory": 1}
    rows = (
        session.connection()
        .execute(
            text("""WITH wanted AS (
        SELECT split_part(row_key,':campaign:',1) AS adv FROM query_snapshot_row
          WHERE snapshot_id=:snapshot AND dimension='campaign'
        UNION SELECT p->>0 FROM query_snapshot_row q CROSS JOIN LATERAL jsonb_array_elements(q.member_keys) p
          WHERE q.snapshot_id=:snapshot AND q.dimension='drama'
    ), ins AS (
        INSERT INTO query_snapshot_name_version(snapshot_id,version_id)
        SELECT :snapshot,h.version_id FROM wanted w JOIN report_name_scope_current h
          ON h.tenant_id=:tenant AND h.advertiser_id=w.adv
        WHERE h.advertiser_id=ANY(:advertisers) RETURNING version_id
    ) SELECT (SELECT count(*) FROM wanted) AS expected,
        COALESCE((SELECT array_agg(version_id) FROM ins),ARRAY[]::uuid[]) AS versions"""),
            {
                "snapshot": snapshot.id,
                "tenant": snapshot.tenant_id,
                "advertisers": snapshot.advertiser_ids,
            },
        )
        .one()
    )
    if rows.expected != len(rows.versions):
        raise _unavailable()
    return {"directory": 1, **{f"name-version:{v}": 1 for v in rows.versions}}


def snapshot_names(
    session: Session,
    *,
    snapshot: QuerySnapshot,
    rows: Sequence[QuerySnapshotRow],
    members: Mapping[str, SnapshotMember],
) -> dict[str, int]:
    dimension = snapshot.filters.get("dimension")
    if dimension == "account":
        account_versions = load_account_versions(
            session,
            tenant_id=snapshot.tenant_id,
            advertisers=snapshot.advertiser_ids,
            version_ids=[
                r.account_membership_version_id
                for r in rows
                if r.account_membership_version_id
            ],
        )
        return {
            "directory": 1,
            **{
                k: v
                for item in account_versions.values()
                for k, v in item.naming_versions.items()
            },
        }
    if dimension not in {"campaign", "drama"}:
        return dict(snapshot.naming_versions)
    name_rows = (
        session.connection()
        .execute(
            text("""SELECT v.id,v.tenant_id,v.advertiser_id,v.kind,v.naming_versions
        FROM query_snapshot_name_version l JOIN report_evidence_version v ON v.id=l.version_id
        WHERE l.snapshot_id=:snapshot"""),
            {"snapshot": snapshot.id},
        )
        .all()
    )
    try:
        expected = {
            UUID(k.removeprefix("name-version:"))
            for k in snapshot.naming_versions
            if k.startswith("name-version:")
        }
    except ValueError:
        raise _unavailable() from None
    if {r.id for r in name_rows} != expected:
        raise _unavailable()
    scopes = {}
    for name_row in name_rows:
        if (
            name_row.tenant_id != snapshot.tenant_id
            or name_row.advertiser_id not in snapshot.advertiser_ids
            or name_row.kind != "name_scope"
        ):
            raise _unavailable()
        scopes.update(name_row.naming_versions)
    keys = {
        f"{ref['advertiser_id']}:{ref['remote_id']}"
        for member in members.values()
        for ref in member.refs
        if ref["kind"] == "campaign"
    }
    return {"directory": 1, **{key: scopes[key] for key in keys if key in scopes}}


def prune_report_evidence(session: Session, *, limit: int = 100) -> None:
    session.connection().execute(
        text("""DELETE FROM report_evidence_version WHERE id IN (
        SELECT v.id FROM report_evidence_version v WHERE v.retired_at<clock_timestamp()-interval '1 hour'
        AND NOT EXISTS(SELECT 1 FROM report_name_scope_current h WHERE h.version_id=v.id)
        AND NOT EXISTS(SELECT 1 FROM material_account_member_current h WHERE h.version_id=v.id)
        AND NOT EXISTS(SELECT 1 FROM material_ad_member_current h WHERE h.version_id=v.id)
        AND NOT EXISTS(SELECT 1 FROM query_snapshot_evidence l WHERE l.version_id=v.id)
        AND NOT EXISTS(SELECT 1 FROM query_snapshot_name_version l WHERE l.version_id=v.id)
        ORDER BY v.retired_at,v.id LIMIT :limit FOR UPDATE SKIP LOCKED)"""),
        {"limit": limit},
    )
