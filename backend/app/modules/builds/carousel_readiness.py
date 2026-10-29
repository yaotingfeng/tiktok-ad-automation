"""轮播整条广告的原子准备；目标图片只读映射，配乐只核验已选 ID。"""

from dataclasses import asdict, dataclass
from uuid import UUID

from sqlmodel import col, select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.builds import CarouselCreative
from app.integrations.tiktok.contracts.common import RemoteCallError
from app.modules.materials.image_distribution import image_ready
from app.modules.materials.models import AccountMaterial, MaterialFile
from app.modules.materials.routes import require_material_route


@dataclass(frozen=True)
class CarouselIssue:
    unit_position: int
    kind: str
    code: str
    identity: str


@dataclass(frozen=True)
class CarouselReadiness:
    attempt_id: UUID | None
    issues: tuple[CarouselIssue, ...]

    @property
    def ready(self):
        return not self.issues


class CarouselNotReady(DomainError):
    def __init__(self, readiness: CarouselReadiness):
        self.readiness = readiness
        self.issues = [asdict(issue) for issue in readiness.issues]
        super().__init__(
            readiness.issues[0].code,
            "原定图片广告尚未完整就绪",
            retryable=all(i.code == "image_refresh_required" for i in readiness.issues),
        )


def resolve_carousel_assets(
    session,
    *,
    context,
    route,
    advertiser_id,
    units,
    prepare_missing=False,
    attempt_id=None,
):
    """最多 50×35 张图，两次批读；失败不返回删减后的单元。"""
    require_material_route(
        session,
        context=context,
        route=route,
        bc_id=route.bc_id,
        advertiser_id=advertiser_id,
        capability="build",
    )
    if not 1 <= len(units) <= 50 or any(u.kind != "CAROUSEL" for u in units):
        raise DomainError("invalid_creative_unit", "冻结图片创意无效")
    ids = {m.material_id for u in units for m in u.members}
    assets = {
        a.material_id: a
        for a in session.exec(
            select(AccountMaterial)
            .where(
                AccountMaterial.tenant_id == context.tenant_id,
                AccountMaterial.bc_id == route.bc_id,
                AccountMaterial.advertiser_id == advertiser_id,
                col(AccountMaterial.material_id).in_(ids),
            )
            .execution_options(populate_existing=True)
        ).all()
    }
    files = {
        f.id: f
        for f in session.exec(
            select(MaterialFile)
            .where(
                MaterialFile.tenant_id == context.tenant_id,
                col(MaterialFile.id).in_(ids),
            )
            .execution_options(populate_existing=True)
        ).all()
    }
    issues = []
    for position, unit in enumerate(units, 1):
        if not unit.music_id:
            issues.append(
                CarouselIssue(
                    position, "music", "carousel_music_missing", str(unit.unit_id)
                )
            )
        for member in unit.members:
            original = files.get(member.material_id)
            if (
                original is None
                or original.media_type != "IMAGE"
                or original.sha256 != member.content_digest
            ):
                issues.append(
                    CarouselIssue(
                        position,
                        "image",
                        "image_content_changed",
                        str(member.material_id),
                    )
                )
            elif not image_ready(assets.get(member.material_id), route):
                issues.append(
                    CarouselIssue(
                        position,
                        "image",
                        "image_refresh_required",
                        str(member.material_id),
                    )
                )
    if issues:
        if prepare_missing and all(i.code == "image_refresh_required" for i in issues):
            from app.modules.materials.image_distribution import prepare_image_targets

            missing = sorted({UUID(i.identity) for i in issues})
            # 广告不修改成员；一次最多补登记100张目标图，其余留给下一片。
            prepared = prepare_image_targets(
                route,
                tuple(missing[:100]),
                (advertiser_id,),
                session=session,
                context=context,
            )
            blocked = {
                str(item.material_id)
                for item in prepared.items
                if item.state == "blocked"
            }
            issues = [
                CarouselIssue(
                    i.unit_position,
                    i.kind,
                    "image_target_unavailable" if i.identity in blocked else i.code,
                    i.identity,
                )
                for i in issues
            ]
        raise CarouselNotReady(CarouselReadiness(attempt_id, tuple(issues)))
    return tuple(
        CarouselCreative(
            image_ids=tuple(assets[m.material_id].image_id for m in u.members),
            music_id=u.music_id,
        )
        for u in units
    )


def verify_carousel_ad(
    route,
    advertiser_id,
    units: tuple[CarouselCreative, ...],
    attempt_id: UUID,
    *,
    gateway,
    budget,
) -> CarouselReadiness:
    """仅当前 attempt 使用，事务外读取精确曲目；空结果与网络失败明确区分。"""
    if route.channel != "OFFICIAL_API":
        raise DomainError("music_channel_unsupported", "当前连接不支持轮播音乐核验")
    ids = tuple(dict.fromkeys(u.music_id for u in units))
    try:
        result = gateway.music.read(
            advertiser_id=advertiser_id, music_ids=ids, budget=budget
        )
    except RemoteCallError as error:
        raise DomainError(
            "carousel_music_read_failed",
            "音乐读取失败，可重试核验原曲目",
            retryable=True,
        ) from error
    found = {item.music_id for item in result.items}
    return CarouselReadiness(
        attempt_id,
        tuple(
            CarouselIssue(i, "music", "carousel_music_unavailable", u.music_id)
            for i, u in enumerate(units, 1)
            if u.music_id not in found
        ),
    )


def body_assets(body):
    try:
        result = tuple(
            CarouselCreative(
                image_ids=tuple(
                    i["web_uri"] for i in row["creative_info"]["image_info"]
                ),
                music_id=row["creative_info"]["music_info"]["music_id"],
            )
            for row in body["creative_list"]
        )
        if not result or any(
            row["creative_info"].get("ad_format") != "CAROUSEL_ADS"
            or "video_info" in row["creative_info"]
            for row in body["creative_list"]
        ):
            raise ValueError()
        return result
    except KeyError, TypeError, ValueError:
        raise DomainError("invalid_build_request", "轮播素材结构无效") from None


def music_proof(claim, units):
    return {
        "attempt_id": str(claim.attempt_id),
        "route": claim.route.model_dump(mode="json"),
        "advertiser_id": claim.advertiser_id,
        "music_ids": [u.music_id for u in units],
    }


def validate_carousel_assets(session, *, step, unit, body, ad, group):
    from app.core.context import TenantContext

    from .creative_units import frozen_ad_creative_units
    from .execution_models import Submission
    from .routes import load_preview_route

    row = session.get(Submission, step.submission_id)
    context = TenantContext(
        tenant_id=step.tenant_id, actor_id=row.actor_id, role="operator"
    )
    route = load_preview_route(session, context=context, preview_id=step.preview_id)
    frozen = frozen_ad_creative_units(
        session,
        tenant_id=step.tenant_id,
        preview_id=step.preview_id,
        drama_id=group.drama_id,
        group_no=group.group_no,
        base_ad_no=ad.base_ad_no,
    )
    expected = resolve_carousel_assets(
        session,
        context=context,
        route=route,
        advertiser_id=unit.advertiser_id,
        units=frozen,
        attempt_id=step.attempt_id,
    )
    actual = body_assets(body)
    if expected != actual:
        raise DomainError(
            "execution_intent_changed", "目标图片顺序或曲目与冻结广告不一致"
        )
    proof = {
        "attempt_id": str(step.attempt_id),
        "route": route.model_dump(mode="json"),
        "advertiser_id": unit.advertiser_id,
        "music_ids": [u.music_id for u in actual],
    }
    if step.resolved.get("carousel_music") != proof:
        raise DomainError(
            "carousel_music_unverified", "当前尝试尚未核验原曲目", retryable=True
        )


def image_preview_readiness(session, *, context, route, advertiser_id, material_ids):
    """整组可大于单广告：固定批查询，只按唯一来源账户核验权限。"""
    from app.modules.materials.models import MaterialAssetOperation
    from app.modules.materials.routes import load_material_route, require_same_route
    from app.modules.materials.schemas import MaterialReadiness

    # 同图可在多个完整创意中复用；仅查询资源身份去重，不修改冻结成员或顺序。
    material_ids = tuple(dict.fromkeys(material_ids))
    if not material_ids or len(material_ids) > 10000:
        raise DomainError("image_distribution_limit", "图片组超出批量上限")

    def blocked(code):
        return MaterialReadiness(state="blocked", path="unavailable", reason_code=code)

    try:
        require_material_route(
            session,
            context=context,
            route=route,
            bc_id=route.bc_id,
            advertiser_id=advertiser_id,
            capability="build",
        )
    except DomainError as error:
        return {i: blocked(error.code) for i in material_ids}
    files = {
        f.id: f
        for f in session.exec(
            select(MaterialFile).where(
                MaterialFile.tenant_id == context.tenant_id,
                col(MaterialFile.id).in_(material_ids),
            )
        ).all()
    }
    from app.modules.materials.ingest_models import TemporaryMaterialObject

    # MaterialFile.original_available 会懒读当前代原件；这里批量读取以免 N+1。
    originals = {
        item.material_id: item
        for item in session.exec(
            select(TemporaryMaterialObject)
            .join(
                MaterialFile,
                (col(MaterialFile.tenant_id) == TemporaryMaterialObject.tenant_id)
                & (col(MaterialFile.bc_id) == TemporaryMaterialObject.bc_id)
                & (col(MaterialFile.id) == TemporaryMaterialObject.material_id)
                & (
                    col(MaterialFile.current_object_generation)
                    == TemporaryMaterialObject.generation
                ),
            )
            .where(
                TemporaryMaterialObject.tenant_id == context.tenant_id,
                TemporaryMaterialObject.bc_id == route.bc_id,
                col(TemporaryMaterialObject.material_id).in_(material_ids),
            )
        ).all()
    }
    assets = session.exec(
        select(AccountMaterial).where(
            AccountMaterial.tenant_id == context.tenant_id,
            AccountMaterial.bc_id == route.bc_id,
            col(AccountMaterial.material_id).in_(material_ids),
            AccountMaterial.media_type == "IMAGE",
        )
    ).all()
    operations = {
        o.material_id: o
        for o in session.exec(
            select(MaterialAssetOperation).where(
                MaterialAssetOperation.tenant_id == context.tenant_id,
                MaterialAssetOperation.bc_id == route.bc_id,
                col(MaterialAssetOperation.material_id).in_(material_ids),
                MaterialAssetOperation.advertiser_id == advertiser_id,
                col(MaterialAssetOperation.superseded_by_id).is_(None),
            )
        ).all()
    }
    targets = {
        a.material_id: a
        for a in assets
        if a.advertiser_id == advertiser_id and image_ready(a, route)
    }
    sources, accounts = set(), {}
    for asset in assets:
        if (
            not image_ready(asset, route)
            or not asset.mid
            or asset.connection_id != route.connection_id
        ):
            continue
        if asset.advertiser_id not in accounts:
            try:
                require_material_route(
                    session,
                    context=context,
                    route=route,
                    bc_id=route.bc_id,
                    advertiser_id=asset.advertiser_id,
                    capability="read",
                )
                accounts[asset.advertiser_id] = True
            except DomainError:
                accounts[asset.advertiser_id] = False
        if accounts[asset.advertiser_id]:
            sources.add(asset.material_id)
    results = {}
    for identity in material_ids:
        file = files.get(identity)
        operation = operations.get(identity)
        if file is None or file.media_type != "IMAGE":
            results[identity] = blocked("image_material_required")
        elif identity in targets:
            from app.modules.materials.repository import asset_public

            results[identity] = MaterialReadiness(
                state="ready",
                path="existing_target",
                mapping=asset_public(targets[identity]),
            )
        elif operation:
            try:
                require_same_route(
                    load_material_route(
                        operation.frozen_route, context=context, bc_id=route.bc_id
                    ),
                    route,
                )
                results[identity] = (
                    blocked(
                        operation.remote_response.get("error_code")
                        or "image_target_unavailable"
                    )
                    if operation.status == "failed"
                    else MaterialReadiness(state="preparable", path="existing_target")
                )
            except DomainError as error:
                results[identity] = blocked(error.code)
        elif identity in sources:
            results[identity] = MaterialReadiness(
                state="preparable", path="share_source"
            )
        elif file.bc_id == route.bc_id and (
            file.storage_state == "stored"
            if file.current_object_generation is None
            else identity in originals and originals[identity].original_available
        ):
            results[identity] = MaterialReadiness(
                state="preparable", path="upload_original"
            )
        else:
            results[identity] = blocked("image_source_unavailable")
    return results
