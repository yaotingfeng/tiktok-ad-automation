"""只读素材展示投影；V2 单元/成员是唯一现行存储，旧表只读历史。"""

from sqlalchemy import and_, func, union_all
from sqlalchemy.orm import aliased
from sqlmodel import col, select

from .preview_models import (
    PreviewAdCreativeUnit,
    PreviewAdMaterial,
    PreviewContract,
    PreviewCreativeUnitItem,
    PreviewGroupCreativeUnit,
    PreviewGroupMaterial,
)


def _projection(legacy, mapping):
    item = PreviewCreativeUnitItem
    # 复用素材展示接口，不把投影结果当创意数或执行意图。
    names = list(legacy.__table__.columns.keys())
    values = {
        name: getattr(mapping, name)
        for name in names
        if name not in {"position", "material_id"}
    }
    values.update(
        position=func.row_number()
        .over(
            partition_by=[
                getattr(mapping, name)
                for name in ("tenant_id", "preview_id", "drama_id", "group_no")
                + (("base_ad_no",) if mapping is PreviewAdCreativeUnit else ())
            ],
            order_by=[mapping.position, col(item.position)],
        )
        .label("position"),
        material_id=item.material_id,
    )
    current = select(*(values[name] for name in names)).join(
        item,
        and_(
            mapping.tenant_id == item.tenant_id,
            mapping.preview_id == item.preview_id,
            mapping.bc_id == item.bc_id,
            mapping.drama_id == item.drama_id,
            mapping.creative_unit_id == item.creative_unit_id,
        ),
    )
    current = current.join(
        PreviewContract,
        and_(
            mapping.tenant_id == PreviewContract.tenant_id,
            mapping.preview_id == PreviewContract.preview_id,
            mapping.bc_id == PreviewContract.bc_id,
            col(PreviewContract.version) == 2,
        ),
    )
    historical = select(legacy).join(
        PreviewContract,
        and_(
            legacy.tenant_id == PreviewContract.tenant_id,
            legacy.preview_id == PreviewContract.preview_id,
            legacy.bc_id == PreviewContract.bc_id,
            col(PreviewContract.version) == 1,
        ),
    )
    return aliased(
        legacy, union_all(historical, current).subquery(), adapt_on_names=True
    )


GroupMaterialView = _projection(PreviewGroupMaterial, PreviewGroupCreativeUnit)
AdMaterialView = _projection(PreviewAdMaterial, PreviewAdCreativeUnit)
