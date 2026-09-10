"""Probe side-effect/redaction contracts; no real storage connection."""

import importlib.util
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def load_script(name):
    path = SCRIPTS / name
    assert path.is_file(), f"{name} is not implemented"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configured():
    return {
        "OBJECT_STORAGE_PROVIDER": "r2",
        "S3_ENDPOINT_URL": "https://test.r2.cloudflarestorage.com",
        "S3_BUCKET": "private-test",
        "S3_ACCESS_KEY_ID": "never-print-access",
        "S3_SECRET_ACCESS_KEY": "never-print-secret",
    }


class ProbeWire:
    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []
        self.key = None
        self.body = b""
        self.created = False
        self.objects = {"existing-user-object": b"do-not-touch"}

    def create_multipart_upload(self, **values):
        self.calls.append(("create", values))
        self.key = values["Key"]
        self.metadata = values["Metadata"]
        assert "ACL" not in values
        self.created = True
        return {"UploadId": "own-upload"}

    def generate_presigned_url(self, operation, **values):
        self.calls.append(("sign", {"operation": operation, **values}))
        return (
            "https://signed.invalid/" + operation + "?signature=never-print-signature"
        )

    def request(self, method, url, **values):
        assert method == "PUT" and "upload_part" in url
        assert int(values["headers"]["Content-Length"]) == len(values["content"])
        self.body = values["content"]
        return Response(200, b"", {"ETag": "part-receipt"})

    def list_parts(self, **values):
        self.calls.append(("list", values))
        return {
            "Parts": [
                {"PartNumber": 1, "Size": len(self.body), "ETag": "part-receipt"}
            ],
            "IsTruncated": False,
        }

    def complete_multipart_upload(self, **values):
        self.calls.append(("complete", values))
        self.objects[self.key] = self.body
        self.created = False
        return {"ETag": "multipart-not-md5"}

    def head_object(self, **values):
        self.calls.append(("head", values))
        if values["Key"] not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {
            "ContentLength": len(self.body),
            "ContentType": "application/octet-stream",
            "Metadata": self.metadata,
        }

    def stream(self, method, url, **_values):
        assert method == "GET" and "get_object" in url
        if self.fail == "get":
            raise RuntimeError(
                "never-print-secret https://signed.invalid/?signature=never-print-signature"
            )
        return Response(200, self.body if self.fail != "digest" else b"wrong")

    def abort_multipart_upload(self, **values):
        self.calls.append(("abort", values))
        self.created = False
        return {}

    def delete_object(self, **values):
        self.calls.append(("delete", values))
        if self.fail == "delete":
            raise RuntimeError("never-print-secret")
        self.objects.pop(values["Key"], None)
        return {}

    def close(self):
        pass


class Response:
    def __init__(self, status, data, headers=None):
        self.status_code, self.data, self.headers = status, data, headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass

    def iter_bytes(self, chunk_size):
        for index in range(0, len(self.data), chunk_size):
            yield self.data[index : index + chunk_size]


def test_default_check_has_no_transport_and_never_prints_values():
    script = load_script("check-r2.py")

    def forbidden(*_args, **_kwargs):
        pytest.fail("default configuration check attempted network")

    result = script.run_check(
        configured(), client_factory=forbidden, http_factory=forbidden
    )
    assert result["config_ready"] is True and result["probe"] == "not_requested"
    assert result["switches"] == {
        "MATERIAL_INGEST_ENABLED": False,
        "MATERIAL_CLEANUP_ENABLED": False,
    }
    assert "never-print" not in str(result)
    missing = script.run_check({})
    assert missing["config_ready"] is False
    assert "S3_ACCESS_KEY_ID" in missing["missing_fields"]
    assert "S3_SECRET_ACCESS_KEY" in missing["missing_fields"]


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://test.r2.cloudflarestorage.com",
        "https://user:password@test.r2.cloudflarestorage.com",
        "https://test.r2.cloudflarestorage.com/path?signature=secret",
    ],
)
def test_invalid_namespace_never_creates_client(endpoint):
    script = load_script("check-r2.py")

    def forbidden(*_args, **_kwargs):
        pytest.fail("invalid namespace reached network")

    result = script.run_check(
        {**configured(), "S3_ENDPOINT_URL": endpoint},
        probe=True,
        client_factory=forbidden,
    )
    assert (
        result["config_ready"] is False
        and "S3_ENDPOINT_URL" in result["invalid_fields"]
    )
    assert "password" not in str(result) and "signature=" not in str(result)


@pytest.mark.parametrize("failure", [None, "get", "digest", "delete"])
def test_probe_cleans_only_its_key_and_reports_incomplete_cleanup(failure):
    script = load_script("check-r2.py")
    wire = ProbeWire(failure)
    result = script.run_check(
        configured(),
        probe=True,
        client_factory=lambda _config: wire,
        http_factory=lambda: wire,
    )
    assert wire.objects["existing-user-object"] == b"do-not-touch"
    assert wire.key.startswith("tiktok-ad-automation/probes/r2/")
    assert len({values["Key"] for _, values in wire.calls if "Key" in values}) == 1
    assert any(name == "delete" for name, _ in wire.calls)
    if failure is None:
        assert result["probe"] == "passed" and result["cleanup"] == "verified"
        assert wire.key not in wire.objects
    else:
        assert result["probe"] == "failed"
        if failure == "delete":
            assert result["cleanup"] == "pending"
    assert "never-print" not in str(result) and "signed.invalid" not in str(result)


def test_cli_errors_are_sanitized(capsys):
    script = load_script("check-r2.py")

    def failing(_config):
        raise RuntimeError("never-print-secret")

    assert script.main(["--probe"], environ=configured(), client_factory=failing) == 1
    assert "never-print-secret" not in capsys.readouterr().out


def test_probe_suppresses_wire_logs_and_retains_unknown_upload_cleanup(
    caplog, monkeypatch
):
    import logging

    script = load_script("check-r2.py")
    wire = ProbeWire()

    def uncertain(method, url, **values):
        wire.request(method, url, **values)
        logging.getLogger("botocore.auth").warning("never-print-secret")
        raise RuntimeError("remote PUT may still be running")

    logger = logging.getLogger("botocore.auth")
    monkeypatch.setattr(logger, "disabled", False)
    http = ProbeWire()
    monkeypatch.setattr(http, "request", uncertain)
    result = script.run_check(
        configured(),
        probe=True,
        client_factory=lambda _config: wire,
        http_factory=lambda: http,
    )
    assert result["probe"] == "failed" and result["cleanup"] == "pending"
    assert "never-print-secret" not in caplog.text
    assert any(name == "abort" for name, _ in wire.calls)


def test_probe_unknown_create_reports_pending_even_after_head404(monkeypatch):
    script = load_script("check-r2.py")
    wire = ProbeWire()

    def uncertain(**values):
        wire.key = values["Key"]
        raise RuntimeError("create receipt lost")

    monkeypatch.setattr(wire, "create_multipart_upload", uncertain)
    result = script.run_check(
        configured(),
        probe=True,
        client_factory=lambda _config: wire,
        http_factory=lambda: wire,
    )
    assert result["probe"] == "failed" and result["cleanup"] == "pending"
    assert not any(name == "abort" for name, _ in wire.calls)
