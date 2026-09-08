from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.providers.schemas import DramaCandidate, ResolvedLink, link_reuse_key


def test_reuse_key_is_canonical_and_keeps_every_scope_dimension():
    tenant, connection = uuid4(), uuid4()
    args = (tenant, connection, "90071992547409931", "drama-1")
    key = link_reuse_key(*args, {"episode": 1, "nested": {"b": 2, "a": "中"}})
    assert len(key) == 64
    assert key == link_reuse_key(*args, {"nested": {"a": "中", "b": 2}, "episode": 1})
    for changed in [
        (uuid4(), connection, args[2], args[3]),
        (tenant, uuid4(), args[2], args[3]),
        (tenant, connection, "app-b", args[3]),
        (tenant, connection, args[2], "drama-2"),
    ]:
        assert key != link_reuse_key(
            *changed, {"episode": 1, "nested": {"b": 2, "a": "中"}}
        )
    assert link_reuse_key(*args, {"episode": 1}) != link_reuse_key(
        *args, {"episode": 2}
    )
    assert link_reuse_key(*args, {"episode": "1"}) != link_reuse_key(
        *args, {"episode": 1}
    )
    assert link_reuse_key(*args, {"channels": ["a", "b"]}) != link_reuse_key(
        *args, {"channels": ["b", "a"]}
    )


@pytest.mark.parametrize(
    "config",
    [
        {"x": float("nan")},
        {"x": float("inf")},
        {1: "bad"},
        {"nested": {2: "bad"}},
        {"tuple": (1, 2)},
        {"secret": object()},
        [],
    ],
)
def test_reuse_key_rejects_non_json_configuration(config):
    with pytest.raises(ValueError):
        link_reuse_key(uuid4(), uuid4(), "app", "drama", config)


def ready_fields():
    return dict(  # noqa: C408
        input_id=uuid4(),
        line_no=1,
        raw_input=" Moon ",
        provider_kind="wangyan",
        connection_id=uuid4(),
        application_id="90071992547409931",
        drama_id=uuid4(),
        external_drama_id="drama-1",
        title="Moon",
        link_id=uuid4(),
        url="https://example.test/link?a=1&b=中",
        protected_base="original_s_name",
        status="ready",
    )


@pytest.mark.parametrize(
    "missing",
    ["drama_id", "external_drama_id", "title", "link_id", "url", "protected_base"],
)
def test_ready_requires_complete_identity_and_attribution(missing):
    fields = ready_fields()
    fields.pop(missing)
    with pytest.raises(ValidationError):
        ResolvedLink(**fields)


@pytest.mark.parametrize("name", ["external_drama_id", "title", "url"])
def test_ready_rejects_blank_required_values(name):
    with pytest.raises(ValidationError):
        ResolvedLink(**(ready_fields() | {name: "   "}))


def test_ready_preserves_raw_identity_url_attribution_and_minis_unknown():
    values = ready_fields()
    result = ResolvedLink(**values)
    assert result.application_id == "90071992547409931"
    assert result.url == values["url"] and result.protected_base == "original_s_name"
    assert result.raw_input == " Moon " and result.tiktok_minis_id is None
    # Empty attribution is only supplied after the adapter proves none is needed.
    assert ResolvedLink(**(values | {"protected_base": ""})).protected_base == ""


@pytest.mark.parametrize(
    "change",
    [
        {"line_no": 0},
        {"line_no": True},
        {"status": "creating"},
        {"application_id": 123},
        {"provider_drama_id": "alias"},
        {"jump_url": "alias"},
    ],
)
def test_public_contract_rejects_invalid_lines_stage_aliases_and_numeric_ids(change):
    with pytest.raises(ValidationError):
        ResolvedLink(**(ready_fields() | change))


def test_unresolved_result_retains_original_line_and_candidates():
    values = ready_fields()
    fields = {
        key: value
        for key, value in values.items()
        if key
        not in {
            "drama_id",
            "external_drama_id",
            "title",
            "link_id",
            "url",
            "protected_base",
        }
    }
    fields["status"] = "needs_resolution"
    fields["candidates"] = [
        DramaCandidate(external_drama_id="en-id", title="Moon", language="en"),
        DramaCandidate(external_drama_id="es-id", title="Moon", language="es"),
    ]
    result = ResolvedLink(**fields)
    assert result.drama_id is None and len(result.candidates) == 2
    assert result.raw_input == " Moon "
