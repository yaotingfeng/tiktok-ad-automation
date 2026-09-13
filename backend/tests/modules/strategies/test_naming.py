import pytest

from app.core.errors import DomainError
from app.modules.strategies.naming import render_names

ARGS = {
    "protected_base": "{b30008/s328302/c3}-The Bond",
    "title": "The Bond",
    "provider_pinyin": "jiashu",
    "external_drama_id": "106001",
    "date_text": "20260913",
    "batch_short_id": "A7K2",
    "group_no": 1,
    "creative_no": 2,
    "max_length": 200,
}


@pytest.mark.parametrize("protected", ["", ARGS["protected_base"]])
def test_unified_name_keeps_external_id_and_automatically_appends_batch(protected):
    base = protected or "jiashu-The Bond"
    names = render_names(**{**ARGS, "protected_base": protected})
    campaign = f"{base}-106001-A7K2"
    assert names == (campaign, campaign + "-g01", campaign + "-g01-sp2")


@pytest.mark.parametrize("protected", ["", ARGS["protected_base"]])
def test_custom_date_order_and_literal_text_apply_to_both_providers(protected):
    base = protected or "jiashu-The Bond"
    names = render_names(
        **{**ARGS, "protected_base": protected},
        template="{provider_drama}-测试-{YYYYMMDD}-{drama_id}",
    )
    assert names[0] == f"{base}-测试-20260913-106001-A7K2"
    assert names[2] == names[0] + "-g01-sp2"


@pytest.mark.parametrize(
    "template",
    [
        "{YYYYMMDD}-{provider_drama}-{drama_id}",
        "前缀-{provider_drama}-{drama_id}",
        "{provider_drama}",
        "{drama_id}",
        "{{provider_drama}}-{drama_id}",
        "{provider_drama}-{drama_id.__class__}",
        "{provider_drama}-{drama_id[0]}",
        "{provider_drama}-{drama_id!r}",
        "{provider_drama}-{drama_id:12}",
        "{provider_drama}-{drama_id}-\n",
        "{provider_drama}-{drama_id}-{random}",
        "{provider_drama}-{drama_id}-{batch_short_id}",
        "{provider_drama}-{drama_id}-{unknown}",
        "{provider_drama}-{drama_id}-{drama_id}",
        "{provider_drama}-{drama_id}-{YYYYMMDD}-{YYYYMMDD}",
        "-{",
        "-{}",
    ],
)
def test_template_requires_both_fields_and_rejects_manual_batch_or_unsafe_variables(
    template,
):
    with pytest.raises(DomainError, match="invalid_name_template"):
        render_names(**ARGS, template=template)


def test_names_never_truncate_protected_prefix():
    with pytest.raises(DomainError, match="name_too_long"):
        render_names(**{**ARGS, "max_length": 15})


def test_inserted_values_are_literal():
    names = render_names(
        **{**ARGS, "protected_base": "", "title": "A {random} Story"},
        template="{provider_drama}-{{literal}}-{drama_id}",
    )
    assert names[0] == "jiashu-A {random} Story-{literal}-106001-A7K2"


@pytest.mark.parametrize("protected", ["", ARGS["protected_base"]])
def test_attribution_prefix_does_not_replace_external_drama_id(protected):
    with pytest.raises(DomainError) as error:
        render_names(**{**ARGS, "protected_base": protected, "external_drama_id": ""})
    assert error.value.code == "naming_context_missing"
