import pytest

from app.core.errors import DomainError
from app.modules.strategies.naming import render_names

ARGS = {
    "protected_base": "{b30008/s328302/c3}-The Bond",
    "title": "The Bond",
    "provider_pinyin": "jiashu",
    "external_drama_id": "106001",
    "date_text": "20260908",
    "batch_short_id": "A7K2",
    "group_no": 1,
    "creative_no": 2,
    "max_length": 200,
}


def test_protected_attribution_is_not_interpreted_as_a_template():
    names = render_names(**ARGS, suffix="-{YYYYMMDD}-{batch_short_id}")
    assert names == (
        "{b30008/s328302/c3}-The Bond-20260908-A7K2",
        "{b30008/s328302/c3}-The Bond-20260908-A7K2-g01",
        "{b30008/s328302/c3}-The Bond-20260908-A7K2-g01-sp2",
    )
    assert (
        render_names(**{**ARGS, "protected_base": ""}, suffix="-{batch_short_id}")[0]
        == "jiashu-The Bond-106001-A7K2"
    )


@pytest.mark.parametrize(
    "suffix",
    [
        "-{protected_base.__class__}",
        "-{batch_short_id[0]}",
        "-{batch_short_id!r}",
        "-{batch_short_id:100}",
        "-{YYYYMMDD}",
        "-{",
        "-{}",
        "-{batch_short_id}-\n",
    ],
)
def test_unapproved_or_malformed_templates_are_rejected(suffix):
    with pytest.raises(DomainError, match="invalid_name_template"):
        render_names(**ARGS, suffix=suffix)


def test_names_never_truncate_protected_prefix():
    with pytest.raises(DomainError, match="name_too_long"):
        render_names(**{**ARGS, "max_length": 15}, suffix="-{batch_short_id}")


@pytest.mark.parametrize(
    "template",
    [
        "{drama_name}-{random}",
        "{drama_id}",
        "{{drama_id}}-{random}",
        "{drama_id}-{random.__class__}",
        "{drama_id}-{random!r}",
        "{drama_id}-{random:12}",
        "{drama_id}-{random}-\n",
        "{unknown}-{drama_id}-{random}",
    ],
)
def test_default_template_requires_identity_and_accepts_only_known_variables(template):
    with pytest.raises(DomainError, match="invalid_name_template"):
        render_names(**ARGS, suffix="-{batch_short_id}", template=template)


def test_custom_default_template_and_inserted_values_are_literal():
    names = render_names(
        **{**ARGS, "protected_base": "", "title": "A {random} Story"},
        suffix="-{batch_short_id}",
        template="{YYYYMMDD}-{{literal}}-{drama_id}-{provider_pinyin}-{drama_name}-{random}",
    )
    assert names[0] == "20260908-{literal}-106001-jiashu-A {random} Story-A7K2"
    assert names[2] == names[0] + "-g01-sp2"


def test_protected_provider_rule_takes_priority_over_default_template():
    assert render_names(
        **ARGS, suffix="-{batch_short_id}", template="{drama_id}-{random}"
    )[0] == ("{b30008/s328302/c3}-The Bond-A7K2")
