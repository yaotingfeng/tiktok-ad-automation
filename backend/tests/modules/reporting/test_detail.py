def test_detail_includes_parent_restrictions(report_case):
    from app.modules.reporting.detail import get_ad_detail

    ref = report_case.seed_campaign("Provider-Drama", "report-account", 1, 2)
    detail = get_ad_detail(
        report_case.session,
        context=report_case.context,
        bc_id=report_case.bc_id,
        ref=ref,
    )
    assert detail.ref.remote_id == ref.remote_id
    assert detail.name == "Provider-Drama"
    assert detail.statuses["operation"] == "ENABLE"


def test_detail_history_reads_management_task_items(report_case):
    from uuid import uuid4

    from app.modules.ad_management.models import (
        ManagementPreview,
        ManagementPreviewItem,
        ManagementTask,
        ManagementTaskItem,
    )
    from app.modules.reporting.detail import get_ad_detail
    from app.modules.reporting.query_models import FrozenSelectionRecord

    session = report_case.session
    ref = report_case.seed_campaign("Provider-Drama", "report-account", 1, 2)
    # 管理预览需要引用一个冻结选择。
    selection = FrozenSelectionRecord(
        tenant_id=report_case.context.tenant_id,
        bc_id=report_case.bc_id,
        actor_id=report_case.context.actor_id,
        snapshot_id=uuid4(),
        advertiser_ids=["report-account"],
        filters={},
        filter_digest="f" * 64,
        publication_versions={},
        naming_versions={},
        refs=[],
        material_uses=[],
        membership_digest="m" * 64,
    )
    session.add(selection)
    session.flush()
    preview = ManagementPreview(
        tenant_id=report_case.context.tenant_id,
        bc_id=report_case.bc_id,
        actor_id=report_case.context.actor_id,
        selection_id=selection.id,
        digest="d" * 64,
        mutation={"field": "status", "mode": "set", "value": "DISABLE"},
    )
    session.add(preview)
    session.flush()
    preview_item_id = uuid4()
    session.add(
        ManagementPreviewItem(
            id=preview_item_id,
            tenant_id=report_case.context.tenant_id,
            preview_id=preview.id,
            ref={
                "tenant_id": str(ref.tenant_id),
                "advertiser_id": ref.advertiser_id,
                "kind": ref.kind,
                "remote_id": ref.remote_id,
            },
            original_value="ENABLE",
            final_value="DISABLE",
        )
    )
    session.flush()
    task = ManagementTask(
        tenant_id=report_case.context.tenant_id,
        bc_id=report_case.bc_id,
        actor_id=report_case.context.actor_id,
        preview_id=preview.id,
        idempotency_key=uuid4(),
        digest="d" * 64,
    )
    session.add(task)
    session.flush()
    session.add(
        ManagementTaskItem(
            tenant_id=report_case.context.tenant_id,
            task_id=task.id,
            preview_item_id=preview_item_id,
            preview_id=preview.id,
            ref={
                "tenant_id": str(ref.tenant_id),
                "advertiser_id": ref.advertiser_id,
                "kind": ref.kind,
                "remote_id": ref.remote_id,
            },
            original_value="ENABLE",
            final_value="DISABLE",
            execution_result="ACCEPTED",
        )
    )
    session.flush()
    detail = get_ad_detail(
        session, context=report_case.context, bc_id=report_case.bc_id, ref=ref
    )
    assert detail.operation_history[0]["field"] == "status"
    assert detail.operation_history[0]["final_value"] == "DISABLE"
