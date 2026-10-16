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


def test_detail_history_excludes_different_bc_and_kind(report_case):
    """不同 BC 和不同 kind 的任务项不会出现在历史中。"""
    from uuid import uuid4

    from app.modules.accounts.models import TenantBC
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

    # 创建另一个 BC 用于负面测试。
    other_bc_id = "bc-other-test"
    session.add(TenantBC(tenant_id=report_case.context.tenant_id, bc_id=other_bc_id))
    session.flush()

    # 为正确 BC 创建一个有效历史项。
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

    preview_correct = ManagementPreview(
        tenant_id=report_case.context.tenant_id,
        bc_id=report_case.bc_id,
        actor_id=report_case.context.actor_id,
        selection_id=selection.id,
        digest="d" * 64,
        mutation={"field": "budget", "mode": "set", "value": "1000"},
    )
    session.add(preview_correct)
    session.flush()

    preview_item_correct_id = uuid4()
    session.add(
        ManagementPreviewItem(
            id=preview_item_correct_id,
            tenant_id=report_case.context.tenant_id,
            preview_id=preview_correct.id,
            ref={
                "tenant_id": str(ref.tenant_id),
                "advertiser_id": ref.advertiser_id,
                "kind": ref.kind,
                "remote_id": ref.remote_id,
            },
            original_value="500",
            final_value="1000",
        )
    )
    session.flush()

    task_correct = ManagementTask(
        tenant_id=report_case.context.tenant_id,
        bc_id=report_case.bc_id,
        actor_id=report_case.context.actor_id,
        preview_id=preview_correct.id,
        idempotency_key=uuid4(),
        digest="d" * 64,
    )
    session.add(task_correct)
    session.flush()

    session.add(
        ManagementTaskItem(
            tenant_id=report_case.context.tenant_id,
            task_id=task_correct.id,
            preview_item_id=preview_item_correct_id,
            preview_id=preview_correct.id,
            ref={
                "tenant_id": str(ref.tenant_id),
                "advertiser_id": ref.advertiser_id,
                "kind": ref.kind,
                "remote_id": ref.remote_id,
            },
            original_value="500",
            final_value="1000",
            execution_result="ACCEPTED",
        )
    )
    session.flush()

    # 负面用例 (a): 不同 BC 的任务项。
    selection_other_bc = FrozenSelectionRecord(
        tenant_id=report_case.context.tenant_id,
        bc_id=other_bc_id,
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
    session.add(selection_other_bc)
    session.flush()

    preview_other_bc = ManagementPreview(
        tenant_id=report_case.context.tenant_id,
        bc_id=other_bc_id,
        actor_id=report_case.context.actor_id,
        selection_id=selection_other_bc.id,
        digest="d" * 64,
        mutation={"field": "status", "mode": "set", "value": "DISABLE"},
    )
    session.add(preview_other_bc)
    session.flush()

    preview_item_other_bc_id = uuid4()
    session.add(
        ManagementPreviewItem(
            id=preview_item_other_bc_id,
            tenant_id=report_case.context.tenant_id,
            preview_id=preview_other_bc.id,
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

    task_other_bc = ManagementTask(
        tenant_id=report_case.context.tenant_id,
        bc_id=other_bc_id,
        actor_id=report_case.context.actor_id,
        preview_id=preview_other_bc.id,
        idempotency_key=uuid4(),
        digest="d" * 64,
    )
    session.add(task_other_bc)
    session.flush()

    session.add(
        ManagementTaskItem(
            tenant_id=report_case.context.tenant_id,
            task_id=task_other_bc.id,
            preview_item_id=preview_item_other_bc_id,
            preview_id=preview_other_bc.id,
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

    # 负面用例 (b): 相同 advertiser_id + remote_id 但不同 kind。
    preview_wrong_kind = ManagementPreview(
        tenant_id=report_case.context.tenant_id,
        bc_id=report_case.bc_id,
        actor_id=report_case.context.actor_id,
        selection_id=selection.id,
        digest="d" * 64,
        mutation={"field": "name", "mode": "set", "value": "Renamed"},
    )
    session.add(preview_wrong_kind)
    session.flush()

    preview_item_wrong_kind_id = uuid4()
    session.add(
        ManagementPreviewItem(
            id=preview_item_wrong_kind_id,
            tenant_id=report_case.context.tenant_id,
            preview_id=preview_wrong_kind.id,
            ref={
                "tenant_id": str(ref.tenant_id),
                "advertiser_id": ref.advertiser_id,
                "kind": "adgroup",  # 不同的 kind
                "remote_id": ref.remote_id,
            },
            original_value="OldName",
            final_value="Renamed",
        )
    )
    session.flush()

    task_wrong_kind = ManagementTask(
        tenant_id=report_case.context.tenant_id,
        bc_id=report_case.bc_id,
        actor_id=report_case.context.actor_id,
        preview_id=preview_wrong_kind.id,
        idempotency_key=uuid4(),
        digest="d" * 64,
    )
    session.add(task_wrong_kind)
    session.flush()

    session.add(
        ManagementTaskItem(
            tenant_id=report_case.context.tenant_id,
            task_id=task_wrong_kind.id,
            preview_item_id=preview_item_wrong_kind_id,
            preview_id=preview_wrong_kind.id,
            ref={
                "tenant_id": str(ref.tenant_id),
                "advertiser_id": ref.advertiser_id,
                "kind": "adgroup",
                "remote_id": ref.remote_id,
            },
            original_value="OldName",
            final_value="Renamed",
            execution_result="ACCEPTED",
        )
    )
    session.flush()

    # 断言：只返回正确 BC 和正确 kind 的历史项。
    detail = get_ad_detail(
        session, context=report_case.context, bc_id=report_case.bc_id, ref=ref
    )
    assert len(detail.operation_history) == 1
    assert detail.operation_history[0]["field"] == "budget"
    assert detail.operation_history[0]["final_value"] == "1000"
