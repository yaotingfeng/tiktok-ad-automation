import pytest

from app.core.errors import DomainError
from app.modules.strategies.naming import render_names

ARGS = {
    "protected_base": "{b30008/s328302/c3}-The Bond",
    "title": "The Bond",
    "date_text": "20260908",
    "batch_short_id": "B7K2M9Q4",
    "group_no": 1,
    "creative_no": 2,
    "max_length": 200,
}


def test_protected_attribution_is_not_interpreted_as_a_template():
    names = render_names(**ARGS, suffix="-{YYYYMMDD}-{batch_short_id}")
    assert names == (
        "{b30008/s328302/c3}-The Bond-20260908-B7K2M9Q4",
        "{b30008/s328302/c3}-The Bond-20260908-B7K2M9Q4-g01",
        "{b30008/s328302/c3}-The Bond-20260908-B7K2M9Q4-g01-sp2",
    )
    assert (
        render_names(**{**ARGS, "protected_base": ""}, suffix="-{batch_short_id}")[0]
        == "The Bond-B7K2M9Q4"
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
