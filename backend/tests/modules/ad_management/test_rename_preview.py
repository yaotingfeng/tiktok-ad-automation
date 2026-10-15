from app.integrations.tiktok.contracts.ads import EntityRef
from app.modules.accounts.models import BCAccountAccess
from app.modules.ad_management.previews import prepare_preview
from app.modules.ad_management.rename import compute_name, name_problem
from app.modules.ad_management.schemas import MutationSpec, RenameRule
from tests.modules.ad_management.conftest import _row, _set_selection


def _rename(**rule):
    return MutationSpec(field="name", mode="set", rename=RenameRule(**rule))


def test_compute_name_rules():
    replace = RenameRule(kind="replace", find="0901", replace_with="1015")
    assert compute_name("嘉书-剧A-0901-0901", replace, "1") == "嘉书-剧A-1015-1015"
    affix = RenameRule(kind="affix", prefix="【停】")
    assert compute_name("【停】嘉书-剧A", affix, "1") == "【停】嘉书-剧A"
    explicit = RenameRule(kind="explicit", names={"1": "新名"})
    assert compute_name("旧", explicit, "1") == "新名"
    assert compute_name("旧", explicit, "2") is None


def test_name_problems():
    assert name_problem("   ") == "name_empty"
    assert name_problem("a" * 513) == "name_too_long"
    assert name_problem("剧名🔥") == "name_has_emoji"
    assert name_problem("嘉书-剧A") is None


def test_rename_trims_whitespace(session, management_env):
    context, bc, account, route, selection = management_env
    ref = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "c-trim")
    row = _row(ref, connection_id=route.connection_id)
    row.name = "嘉书-剧A"
    session.add(row)
    session.flush()
    _set_selection(selection, [ref])
    p = prepare_preview(
        session,
        context,
        bc.bc_id,
        selection.id,
        _rename(kind="explicit", names={"c-trim": " 嘉书-剧A　"}),
    )
    assert p.items[0].execution_result == "NO_CHANGE"


def test_rename_batch_duplicates_all_rejected(session, management_env):
    context, bc, account, route, selection = management_env
    refs = []
    for remote_id, name in (("c1", "嘉书-剧A-0901"), ("c2", "嘉书-剧A-0902")):
        ref = EntityRef(context.tenant_id, account.advertiser_id, "campaign", remote_id)
        row = _row(ref, connection_id=route.connection_id)
        row.name = name
        session.add(row)
        refs.append(ref)
    session.flush()
    _set_selection(selection, refs)
    p = prepare_preview(
        session,
        context,
        bc.bc_id,
        selection.id,
        _rename(kind="explicit", names={"c1": "嘉书-剧A", "c2": "嘉书-剧A"}),
    )
    assert {item.reason for item in p.items} == {"name_duplicate_in_batch"}
    assert p.counts.targets == 0


def test_rename_conflicts_with_existing_account_name(session, management_env):
    context, bc, account, route, selection = management_env
    target = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "c-a")
    other = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "c-b")
    for ref, name in ((target, "嘉书-剧A"), (other, "嘉书-剧B")):
        row = _row(ref, connection_id=route.connection_id)
        row.name = name
        session.add(row)
    session.flush()
    _set_selection(selection, [target])
    p = prepare_preview(
        session,
        context,
        bc.bc_id,
        selection.id,
        _rename(kind="replace", find="剧A", replace_with="剧B"),
    )
    assert p.items[0].reason == "name_duplicate_in_account"


def test_rename_reports_drama_change_and_non_campaign(session, management_env):
    context, bc, account, route, selection = management_env
    campaign = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "c-d")
    group = EntityRef(context.tenant_id, account.advertiser_id, "adgroup", "g-d")
    row = _row(campaign, connection_id=route.connection_id)
    row.name = "嘉书-剧A-0901"
    session.add_all(
        [row, _row(group, parent=campaign, connection_id=route.connection_id)]
    )
    session.flush()
    _set_selection(selection, [campaign, group])
    p = prepare_preview(
        session,
        context,
        bc.bc_id,
        selection.id,
        _rename(kind="replace", find="剧A", replace_with="剧C"),
    )
    by_kind = {item.ref.kind: item for item in p.items}
    assert by_kind["campaign"].final_value == "嘉书-剧C-0901"
    assert (
        by_kind["campaign"].drama_name == "剧C"
        and by_kind["campaign"].drama_changed is True
    )
    assert by_kind["adgroup"].reason == "rename_campaign_only"


def test_rename_without_capability_unsupported(session, management_env):
    context, bc, account, route, selection = management_env
    ref = EntityRef(context.tenant_id, account.advertiser_id, "campaign", "c-cap")
    session.add(_row(ref, connection_id=route.connection_id))
    # 没有搭建权限（例如只读角色）时不能改名。
    session.get(
        BCAccountAccess,
        (context.tenant_id, bc.bc_id, account.advertiser_id, route.connection_id),
    ).can_build = False
    session.flush()
    _set_selection(selection, [ref])
    p = prepare_preview(
        session, context, bc.bc_id, selection.id, _rename(kind="affix", suffix="_1015")
    )
    assert p.items[0].reason == "management_capability_unverified"


def _campaigns(session, context, account, route, names):
    refs = []
    for remote_id, name in names:
        ref = EntityRef(context.tenant_id, account.advertiser_id, "campaign", remote_id)
        row = _row(ref, connection_id=route.connection_id)
        row.name = name
        session.add(row)
        refs.append(ref)
    return refs


def test_rename_swap_rejected_for_both_rows(session, management_env):
    context, bc, account, route, selection = management_env
    refs = _campaigns(
        session, context, account, route, (("s1", "嘉书-剧A"), ("s2", "嘉书-剧B"))
    )
    session.flush()
    _set_selection(selection, refs)
    p = prepare_preview(
        session,
        context,
        bc.bc_id,
        selection.id,
        _rename(kind="explicit", names={"s1": "嘉书-剧B", "s2": "嘉书-剧A"}),
    )
    assert [item.reason for item in p.items] == ["name_duplicate_in_account"] * 2
    assert p.counts.targets == 0


def test_rename_collides_with_unchanged_row_name(session, management_env):
    context, bc, account, route, selection = management_env
    refs = _campaigns(
        session, context, account, route, (("k1", "嘉书-剧A"), ("k2", "嘉书-剧K"))
    )
    session.flush()
    # k1 保持原名“嘉书-剧A”（不变），k2 改成同名必须被拒绝。
    _set_selection(selection, refs)
    p = prepare_preview(
        session,
        context,
        bc.bc_id,
        selection.id,
        _rename(kind="explicit", names={"k1": "嘉书-剧A", "k2": "嘉书-剧A"}),
    )
    by_id = {item.ref.remote_id: item for item in p.items}
    assert by_id["k1"].execution_result == "NO_CHANGE"
    assert by_id["k2"].reason == "name_duplicate_in_account"


def test_rename_affix_suffix_exposes_old_name(session, management_env):
    context, bc, account, route, selection = management_env
    refs = _campaigns(session, context, account, route, (("x1", "嘉书-剧A-0901"),))
    session.flush()
    _set_selection(selection, refs)
    p = prepare_preview(
        session, context, bc.bc_id, selection.id, _rename(kind="affix", suffix="_1015")
    )
    item = p.items[0]
    assert item.execution_result == "PENDING"
    assert item.original_value == "嘉书-剧A-0901"
    assert item.final_value == "嘉书-剧A-0901_1015"
    assert item.drama_name == "剧A" and item.drama_changed is False
    assert p.counts.targets == 1
