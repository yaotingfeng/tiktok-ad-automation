"""Read-only R2 configuration check; --probe explicitly writes one random test key.

Never prints environment values, credentials, provider errors or signed URLs.
No bucket creation, policy changes, namespace scan or existing-object access.
"""

import argparse

# Command-line stdout intentionally emits only sanitized JSON.
# ruff: noqa: T201
import hashlib
import json
import logging
import os
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

REQUIRED = (
    "OBJECT_STORAGE_PROVIDER",
    "S3_ENDPOINT_URL",
    "S3_BUCKET",
    "S3_ACCESS_KEY_ID",
    "S3_SECRET_ACCESS_KEY",
)
SWITCHES = ("MATERIAL_INGEST_ENABLED", "MATERIAL_CLEANUP_ENABLED")
NUMBERS = {
    "MATERIAL_STORAGE_GLOBAL_BYTES": (8 * 1024**3, 1, None),
    "MATERIAL_STORAGE_TENANT_BYTES": (2 * 1024**3, 1, None),
    "MATERIAL_URL_MAX_UPLOAD_BYTES": (256 * 1024**2, 1, None),
    "MATERIAL_PART_URL_SECONDS": (900, 60, 900),
    "MATERIAL_INGEST_URL_SECONDS": (7200, 60, 7200),
    "MATERIAL_VALIDATION_SECONDS": (300, 30, 600),
    "MATERIAL_ABANDON_SECONDS": (86400, 3600, None),
}
PLACEHOLDERS = {
    "changethis",
    "change-me",
    "minioadmin",
    "example",
    "your-access-key",
    "your-secret-key",
}


def configuration(environ: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    config = {name: environ.get(name, "").strip() for name in REQUIRED}
    missing = [
        name
        for name, value in config.items()
        if not value or value.lower() in PLACEHOLDERS
    ]
    invalid = []
    if config["OBJECT_STORAGE_PROVIDER"] and config["OBJECT_STORAGE_PROVIDER"] != "r2":
        invalid.append("OBJECT_STORAGE_PROVIDER")
    if config["S3_ENDPOINT_URL"]:
        try:
            endpoint = urlsplit(config["S3_ENDPOINT_URL"])
            valid = (
                endpoint.scheme == "https"
                and endpoint.hostname is not None
                and endpoint.hostname.endswith(".r2.cloudflarestorage.com")
                and endpoint.hostname != "r2.cloudflarestorage.com"
                and endpoint.username is None
                and endpoint.password is None
                and endpoint.port in (None, 443)
                and endpoint.path in ("", "/")
                and not endpoint.query
                and not endpoint.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            invalid.append("S3_ENDPOINT_URL")
    switches = {}
    for name in SWITCHES:
        value = environ.get(name, "false").strip().lower()
        if value not in {"true", "false", "1", "0"}:
            invalid.append(name)
        switches[name] = value in {"true", "1"}
    limits = {}
    for name, (default, minimum, maximum) in NUMBERS.items():
        try:
            value = int(environ.get(name, str(default)))
            if value < minimum or (maximum is not None and value > maximum):
                raise ValueError
            limits[name] = value
        except ValueError:
            invalid.append(name)
    return config, {
        "config_ready": not missing and not invalid,
        "missing_fields": sorted(missing),
        "invalid_fields": sorted(invalid),
        "switches": switches,
        "limits": limits,
        "probe": "not_requested",
        "live_acceptance": "pending",
    }


def make_client(config: Mapping[str, str]) -> Any:
    import boto3
    from botocore.config import Config

    # SDK wire debug logs can contain Authorization and signed query strings.
    for name in ("boto3", "botocore", "urllib3"):
        logging.getLogger(name).disabled = True
        for logger_name in logging.Logger.manager.loggerDict:
            if logger_name.startswith(name + "."):
                logging.getLogger(logger_name).disabled = True
    return boto3.client(
        "s3",
        endpoint_url=config["S3_ENDPOINT_URL"],
        region_name="auto",
        aws_access_key_id=config["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=config["S3_SECRET_ACCESS_KEY"],
        config=Config(
            signature_version="s3v4",
            connect_timeout=5,
            read_timeout=15,
            retries={"total_max_attempts": 1},
            s3={"addressing_style": "path"},
        ),
    )


def make_http() -> Any:
    import httpx

    for name in ("httpx", "httpcore"):
        logging.getLogger(name).disabled = True
        for logger_name in logging.Logger.manager.loggerDict:
            if logger_name.startswith(name + "."):
                logging.getLogger(logger_name).disabled = True
    return httpx.Client(
        timeout=httpx.Timeout(15, connect=5), follow_redirects=False, trust_env=False
    )


def error_code(error: Exception) -> str:
    response = getattr(error, "response", {})
    return (
        str(response.get("Error", {}).get("Code", ""))
        if isinstance(response, dict)
        else ""
    )


def require(condition: bool) -> None:
    if not condition:
        raise ValueError("probe step did not verify")


def probe_object(
    config: Mapping[str, str],
    *,
    client_factory: Callable[..., Any],
    http_factory: Callable[..., Any],
) -> dict[str, Any]:
    run_id = uuid4().hex
    key = f"tiktok-ad-automation/probes/r2/{run_id}/payload.bin"
    bucket = config["S3_BUCKET"]
    payload = b"private-r2-compatibility-probe\n" * 32
    digest = hashlib.sha256(payload).hexdigest()
    metadata = {
        "application": "tiktok-ad-automation",
        "probe-id": run_id,
        "sha256": digest,
    }
    client = http = None
    upload_id = None
    completed = False
    stage = "client"
    result: dict[str, Any] = {
        "probe": "failed",
        "cleanup": "not_started",
        "run_id": run_id,
        "verified_steps": [],
    }
    try:
        client = client_factory(config)
        http = http_factory()
        stage = "create_multipart"
        response = client.create_multipart_upload(
            Bucket=bucket,
            Key=key,
            ContentType="application/octet-stream",
            Metadata=metadata,
        )
        upload_id = response.get("UploadId")
        require(isinstance(upload_id, str) and bool(upload_id))
        result["verified_steps"].append(stage)
        stage = "signed_put"
        url = client.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": bucket,
                "Key": key,
                "UploadId": upload_id,
                "PartNumber": 1,
                "ContentLength": len(payload),
            },
            ExpiresIn=60,
        )
        response = http.request(
            "PUT", url, content=payload, headers={"Content-Length": str(len(payload))}
        )
        require(response.status_code == 200)
        etag = response.headers.get("ETag")
        require(isinstance(etag, str) and bool(etag))
        result["verified_steps"].append(stage)
        stage = "list_parts"
        response = client.list_parts(
            Bucket=bucket, Key=key, UploadId=upload_id, MaxParts=2
        )
        parts = response.get("Parts", [])
        require(
            response.get("IsTruncated") is False
            and len(parts) == 1
            and parts[0].get("PartNumber") == 1
            and parts[0].get("Size") == len(payload)
            and parts[0].get("ETag") == etag
        )
        result["verified_steps"].append(stage)
        stage = "complete"
        client.complete_multipart_upload(
            Bucket=bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": [{"PartNumber": 1, "ETag": etag}]},
        )
        completed = True
        result["verified_steps"].append(stage)
        stage = "head"
        response = client.head_object(Bucket=bucket, Key=key)
        require(
            response.get("ContentLength") == len(payload)
            and response.get("ContentType") == "application/octet-stream"
            and response.get("Metadata") == metadata
        )
        result["verified_steps"].append(stage)
        stage = "signed_get"
        url = client.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=60
        )
        received = bytearray()
        with http.stream("GET", url) as response:
            require(response.status_code == 200)
            for chunk in response.iter_bytes(chunk_size=1024):
                require(len(received) + len(chunk) <= len(payload))
                received.extend(chunk)
        require(
            len(received) == len(payload)
            and hashlib.sha256(received).hexdigest() == digest
        )
        result["verified_steps"].append(stage)
        result["probe"] = "passed"
    except Exception:
        # Even ValueError/HTTP/SDK exception repr can include secrets. Never print it.
        result["failed_step"] = stage
    finally:
        clean = client is not None
        if client is not None:
            if upload_id and not completed:
                try:
                    client.abort_multipart_upload(
                        Bucket=bucket, Key=key, UploadId=upload_id
                    )
                except Exception as error:
                    if error_code(error) not in {"NoSuchUpload", "404"}:
                        clean = False
            # An unknown create has no upload ID; never claim multipart cleanup.
            if not upload_id:
                clean = False
            try:
                client.delete_object(Bucket=bucket, Key=key)
                result["verified_steps"].append("delete")
                try:
                    client.head_object(Bucket=bucket, Key=key)
                except Exception as error:
                    if error_code(error) not in {"404", "NoSuchKey", "NotFound"}:
                        raise
                else:
                    raise ValueError("object remains")
                result["verified_steps"].append("head_404")
            except Exception:
                clean = False
            # A failed PUT/Complete may still be in flight. Immediate abort/404
            # alone is not proof that its remote operation has terminated.
            if not completed and result.get("failed_step") in {
                "signed_put",
                "complete",
            }:
                clean = False
        result["cleanup"] = "verified" if clean else "pending"
        if not clean:
            result["probe"] = "failed"
        for resource in (http, client):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
    return result


def run_check(
    environ: Mapping[str, str],
    *,
    probe: bool = False,
    client_factory: Callable[..., Any] = make_client,
    http_factory: Callable[..., Any] = make_http,
) -> dict[str, Any]:
    config, result = configuration(environ)
    if probe and result["config_ready"]:
        previous_logging = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        try:
            result.update(
                probe_object(
                    config, client_factory=client_factory, http_factory=http_factory
                )
            )
        finally:
            logging.disable(previous_logging)
    elif probe:
        result["probe"] = "blocked"
    return result


def main(
    argv: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[..., Any] = make_client,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Explicitly create and clean one random application test key",
    )
    args = parser.parse_args(argv)
    result = run_check(
        os.environ if environ is None else environ,
        probe=args.probe,
        client_factory=client_factory,
    )
    print(json.dumps(result, sort_keys=True))
    return (
        0
        if result["config_ready"] and result["probe"] in {"not_requested", "passed"}
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
