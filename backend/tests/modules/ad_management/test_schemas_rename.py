import pytest
from pydantic import ValidationError

from app.modules.ad_management.schemas import MutationSpec, RenameRule


def test_rename_requires_rule_and_set_mode():
    with pytest.raises(ValidationError):
        MutationSpec(field="name", mode="set")
    with pytest.raises(ValidationError):
        MutationSpec(
            field="name", mode="add", rename=RenameRule(kind="affix", prefix="x")
        )
    spec = MutationSpec(
        field="name", mode="set", rename=RenameRule(kind="affix", prefix="【停】")
    )
    assert spec.value is None


def test_rename_rule_shapes():
    with pytest.raises(ValidationError):
        RenameRule(kind="replace", find="")
    with pytest.raises(ValidationError):
        RenameRule(kind="affix")
    with pytest.raises(ValidationError):
        RenameRule(kind="explicit", names={})
    with pytest.raises(ValidationError):
        RenameRule(kind="explicit", names={str(i): "n" for i in range(501)})
    assert RenameRule(kind="replace", find="0901", replace_with="").replace_with == ""


def test_other_fields_reject_rename_and_require_value():
    with pytest.raises(ValidationError):
        MutationSpec(
            field="budget",
            mode="set",
            value=10,
            rename=RenameRule(kind="affix", prefix="x"),
        )
    with pytest.raises(ValidationError):
        MutationSpec(field="budget", mode="set")
