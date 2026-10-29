"""测试显式连接单元和唯一视频成员，断言当前事实而非退役草稿映射。"""

from sqlalchemy import and_, literal
from sqlmodel import select

from app.modules.builds.image_composition_models import DraftCreativeUnit as Unit
from app.modules.builds.image_composition_models import DraftCreativeUnitItem as Item

VIDEO_SOURCE = (
    select(
        Unit.tenant_id,
        Unit.draft_id,
        Unit.drama_id,
        Unit.bc_id,
        Unit.generation,
        Unit.id.label("unit_id"),
        Unit.position,
        Item.material_id,
        literal(1).label("group_no"),
    )
    .join(
        Item,
        and_(
            Item.tenant_id == Unit.tenant_id,
            Item.draft_id == Unit.draft_id,
            Item.generation == Unit.generation,
            Item.unit_id == Unit.id,
        ),
    )
    .where(Unit.kind == "VIDEO", Unit.generation == 1)
    .subquery()
)
