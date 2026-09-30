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
