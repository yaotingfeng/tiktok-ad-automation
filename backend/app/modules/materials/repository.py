import base64
import hashlib
import hmac
import json
from uuid import UUID

from sqlalchemy import and_, func, or_
from sqlalchemy import select as sa_select
from sqlmodel import Session, col, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.core.pagination import Page
from app.modules.accounts.models import TenantBC
from app.modules.materials.models import AccountMaterial, MaterialFile
from app.modules.materials.schemas import AccountAsset, MaterialCandidate
from app.modules.tenants.permissions import require_tenant


def require_material_scope(
    session: Session, *, context: TenantContext, bc_id: str
) -> None:
    require_tenant(
        session, actor_id=context.actor_id, tenant_id=context.tenant_id, action="read"
    )
    if (
        session.get(TenantBC, (context.tenant_id, bc_id), populate_existing=True)
        is None
    ):
        raise DomainError("account_not_in_bc", "当前租户没有该 BC")


def asset_public(asset: AccountMaterial) -> AccountAsset:
    return AccountAsset(
        asset_id=asset.id, **asset.model_dump(exclude={"id", "tenant_id"})
    )


def encode_material_cursor(*, scope: dict[str, str], name: str, identity: UUID) -> str:
    payload = json.dumps(
        [scope, name, str(identity)], ensure_ascii=False, separators=(",", ":")
    ).encode()
    signature = hmac.new(settings.SECRET_KEY.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature + payload).decode()


def decode_material_cursor(cursor: str, *, scope: dict[str, str]) -> tuple[str, UUID]:
    try:
        if not cursor or len(cursor) > 8192:
            raise ValueError
        raw = base64.b64decode(cursor, altchars=b"-_", validate=True)
        signature, payload = raw[:32], raw[32:]
        if not hmac.compare_digest(
            signature,
            hmac.new(settings.SECRET_KEY.encode(), payload, hashlib.sha256).digest(),
        ):
            raise ValueError
        values = json.loads(payload)
        if not isinstance(values, list) or len(values) != 3:
            raise ValueError
        owner, name, identity = values
        if (
            owner != scope
            or not isinstance(name, str)
            or len(name) > 1000
            or not isinstance(identity, str)
        ):
            raise ValueError
        return name, UUID(identity)
    except ValueError, TypeError, UnicodeError:
        raise DomainError("invalid_cursor", "素材游标无效或不属于当前查询") from None


def matching_page(
    session: Session,
    *,
    context: TenantContext,
    bc_id: str,
    title: str,
    cursor: str | None = None,
) -> Page[MaterialCandidate]:
    require_material_scope(session, context=context, bc_id=bc_id)
    needle = title.strip().casefold()
    if not needle:
        raise DomainError("empty_title", "请输入完整剧目名称")
    if len(needle) > 1000:
        raise DomainError("invalid_title", "剧目名称过长")
    scope = {
        "tenant": str(context.tenant_id),
        "bc": bc_id,
        "title": hashlib.sha256(needle.encode()).hexdigest(),
    }
    verified_asset = (
        select(AccountMaterial.id)
        .where(
            AccountMaterial.tenant_id == context.tenant_id,
            AccountMaterial.bc_id == bc_id,
            AccountMaterial.material_id == MaterialFile.id,
            AccountMaterial.status == "available",
            col(AccountMaterial.verified_at).is_not(None),
        )
        .exists()
    )
    statement = select(MaterialFile).where(
        MaterialFile.tenant_id == context.tenant_id,
        MaterialFile.bc_id == bc_id,
        MaterialFile.storage_state != "receiving",
        or_(col(MaterialFile.storage_state) == "stored", verified_asset),
        col(MaterialFile.file_name_folded).contains(needle, autoescape=True),
    )
    name_order = col(MaterialFile.file_name).collate("C")
    if cursor is not None:
        name, identity = decode_material_cursor(cursor, scope=scope)
        statement = statement.where(
            or_(
                name_order > name,
                and_(name_order == name, col(MaterialFile.id) > identity),
            )
        )
    rows = session.exec(
        statement.order_by(name_order, col(MaterialFile.id)).limit(101)
    ).all()
    materials = rows[:100]
    # Candidate pages carry one representative verified location per file. The
    # readiness/distribution lookup and paged detail API inspect all mappings;
    # a file distributed to 10,000 accounts must not expand this response 10,000x.
    assets: dict[UUID, AccountMaterial] = {}
    if materials:
        ranked = (
            sa_select(
                col(AccountMaterial.id),
                func.row_number()
                .over(
                    partition_by=col(AccountMaterial.material_id),
                    order_by=(
                        col(AccountMaterial.advertiser_id),
                        col(AccountMaterial.id),
                    ),
                )
                .label("position"),
            )
            .where(
                col(AccountMaterial.tenant_id) == context.tenant_id,
                col(AccountMaterial.bc_id) == bc_id,
                col(AccountMaterial.material_id).in_([row.id for row in materials]),
                col(AccountMaterial.status) == "available",
                col(AccountMaterial.verified_at).is_not(None),
            )
            .subquery()
        )
        candidates = session.exec(
            select(AccountMaterial)
            .join(ranked, col(AccountMaterial.id) == ranked.c.id)
            .where(ranked.c.position == 1)
        ).all()
        assets = {asset.material_id: asset for asset in candidates}
    items = [
        MaterialCandidate(
            material_id=row.id,
            file_name=row.file_name,
            bc_id=row.bc_id,
            original_available=row.storage_state == "stored",
            source_assets=[asset_public(assets[row.id])] if row.id in assets else [],
        )
        for row in materials
    ]
    return Page(
        items=items,
        next_cursor=encode_material_cursor(
            scope=scope, name=materials[-1].file_name, identity=materials[-1].id
        )
        if len(rows) > 100
        else None,
    )
