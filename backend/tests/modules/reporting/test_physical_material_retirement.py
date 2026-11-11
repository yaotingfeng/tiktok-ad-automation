"""物理退休必须先保全官方身份，旧表先退休，旧列仍由前置脚本在线保全。"""

from sqlalchemy import inspect, text
from sqlmodel import SQLModel

from app.modules.ads.models import AdMaterialReference
from app.modules.reporting.read_models.ddl import trigger_statements


def test_current_schema_has_no_retired_tables_and_keeps_legacy_array(session):
    retired = {"material_report_day", "material_post_link", "material_link_scan"}
    assert not retired.intersection(inspect(session.connection()).get_table_names())
    assert not retired.intersection(SQLModel.metadata.tables)
    assert "creative_ids" not in AdMaterialReference.__table__.c
    assert "creative_ids" in {
        c["name"]
        for c in inspect(session.connection()).get_columns("ad_material_reference")
    }
    assert not any(name in "\n".join(trigger_statements()) for name in retired)
    assert (
        session.execute(
            text(
                "SELECT count(*) FROM pg_proc WHERE proname LIKE 'qrm_material_post_link_%' OR proname LIKE 'qrm_material_report_day_%'"
            )
        ).scalar_one()
        == 0
    )
