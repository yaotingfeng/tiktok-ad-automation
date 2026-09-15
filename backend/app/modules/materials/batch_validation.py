"""一次短事务内复用来源及权限事实，不跨请求保留授权结论。"""

from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID

from sqlmodel import Session, col, select

from app.core.context import TenantContext
from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.modules.accounts.connection_models import ConnectionAuthorization
from app.modules.accounts.models import BCAccountAccess
from app.modules.accounts.routing import Capability, _fresh

from .models import AccountMaterial, MaterialFile
from .remote_sources import legal_source_grant
from .repository import require_material_scope
from .routes import require_material_route


class BatchSourceVerifier:
    def __init__(
        self,
        session: Session,
        *,
        context: TenantContext,
        source_bc_id: str,
        source_asset_ids: set[UUID],
        materials: Iterable[MaterialFile],
    ) -> None:
        self.session, self.context = session, context
        self.transaction = session.get_transaction()
        self.source_bc_id = source_bc_id
        self.checked: dict[
            tuple[str, str, str, Capability], tuple[datetime | None, datetime | None]
        ] = {}
        require_material_scope(session, context=context, bc_id=source_bc_id)
        # 保留原查询的当前合法授权 EXISTS；批读不是信任冻结 VID 或旧 mapping。
        self.sources = {
            row.id: row
            for row in session.exec(
                select(AccountMaterial)
                .where(
                    AccountMaterial.tenant_id == context.tenant_id,
                    AccountMaterial.bc_id == source_bc_id,
                    col(AccountMaterial.id).in_(source_asset_ids),
                    AccountMaterial.status == "available",
                    col(AccountMaterial.verified_at).is_not(None),
                    col(AccountMaterial.video_id) != "",
                    legal_source_grant(context=context, bc_id=source_bc_id),
                )
                .execution_options(populate_existing=True)
            ).all()
        }
        self.materials = {row.id: row for row in materials}
        missing = {
            source.material_id for source in self.sources.values()
        } - self.materials.keys()
        if missing:
            self.materials.update(
                (row.id, row)
                for row in session.exec(
                    select(MaterialFile)
                    .where(
                        MaterialFile.tenant_id == context.tenant_id,
                        col(MaterialFile.id).in_(missing),
                    )
                    .execution_options(populate_existing=True)
                ).all()
            )

    def _current(self, session: Session, context: TenantContext) -> None:
        if (
            session is not self.session
            or context != self.context
            or self.transaction is None
            or session.get_transaction() is not self.transaction
            or not self.transaction.is_active
        ):
            raise DomainError("material_claim_changed", "批次核验事务已结束")

    def source(
        self,
        session: Session,
        *,
        context: TenantContext,
        bc_id: str,
        material_id: UUID,
        source_asset_id: UUID,
    ) -> AccountMaterial | None:
        self._current(session, context)
        row = self.sources.get(source_asset_id)
        if bc_id != self.source_bc_id or row is None or row.material_id != material_id:
            return None
        return row

    def require_route(
        self,
        session: Session,
        *,
        context: TenantContext,
        route: FrozenTikTokRoute,
        bc_id: str,
        advertiser_id: str,
        capability: Capability,
    ) -> None:
        self._current(session, context)
        key = (route.model_dump_json(), bc_id, advertiser_id, capability)
        if key not in self.checked:
            # build/read/upload 分开核验，不以某种能力代替另一种。
            require_material_route(
                session,
                context=context,
                route=route,
                bc_id=bc_id,
                advertiser_id=advertiser_id,
                capability=capability,
            )
            authorization_time, grant_time = session.exec(
                select(ConnectionAuthorization.verified_at, BCAccountAccess.checked_at)
                .join(
                    BCAccountAccess,
                    col(BCAccountAccess.connection_id)
                    == ConnectionAuthorization.connection_id,
                )
                .where(
                    ConnectionAuthorization.tenant_id == context.tenant_id,
                    ConnectionAuthorization.connection_id == route.connection_id,
                    ConnectionAuthorization.authorization_revision
                    == route.authorization_revision,
                    BCAccountAccess.tenant_id == context.tenant_id,
                    BCAccountAccess.bc_id == bc_id,
                    BCAccountAccess.advertiser_id == advertiser_id,
                )
            ).one()
            self.checked[key] = (authorization_time, grant_time)
        self.recheck_freshness()

    def recheck_freshness(self) -> None:
        # 即使同一短事务跨过证据有效期，也不能复用刚才的授权结论。
        self._current(self.session, self.context)
        now = datetime.now(UTC)
        if any(
            not _fresh(stamp, now) for times in self.checked.values() for stamp in times
        ):
            raise DomainError("route_evidence_stale", "账户授权证据需要重新检查")
