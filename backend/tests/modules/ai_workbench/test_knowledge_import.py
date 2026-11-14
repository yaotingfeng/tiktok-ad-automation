from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import uuid4

import pytest


def importer():
    path = Path(__file__).resolve().parents[4] / "scripts/import-ai-knowledge-pack.py"
    spec = spec_from_file_location("knowledge_import", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_importer_reuses_asset_and_updates_explicit_version():
    module = importer()
    asset = str(uuid4())
    knowledge = str(uuid4())
    body = {"key": "policy", "scope": "PUBLIC", "title": "policy", "body": "v1"}
    calls = []

    def send(method, path, payload=None):
        calls.append((method, path, payload))
        if method == "GET":
            return [{**body, "id": knowledge, "asset_id": asset, "version": 1}]
        return {"id": str(uuid4()), "asset_id": asset, "version": 2}

    result = module.import_pack(
        body,
        origin="https://example.test",
        tenant_id=str(uuid4()),
        token="local-test",
        request=send,
    )
    assert result["status"] == "unchanged" and len(calls) == 1
    result = module.import_pack(
        {**body, "body": "v2"},
        origin="https://example.test",
        tenant_id=str(uuid4()),
        token="local-test",
        request=send,
    )
    assert result["status"] == "updated" and calls[-1][1].endswith(f"/{asset}/versions")


@pytest.mark.parametrize(
    "url",
    [
        "http://external.test",
        "https://user:secret@example.test",
        "https://example.test/path",
        "https://example.test?x=1",
    ],
)
def test_importer_rejects_unsafe_origins_without_transmitting(url):
    with pytest.raises(ValueError):
        importer().api_origin(url)


def test_real_pack_comparison_ignores_write_only_sharing_review():
    import json

    path = (
        Path(__file__).resolve().parents[4]
        / "docs/knowledge-packs-new-drama-subsidy-q3-q4.json"
    )
    body = json.loads(path.read_text())
    existing = {key: value for key, value in body.items() if key != "sharing_review"}
    assert importer().same_payload(existing, body)
    existing["body"] = "changed policy"
    assert not importer().same_payload(existing, body)
