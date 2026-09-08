import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from app.core.config import Settings, configured_app_fields
from app.core.errors import DomainError


def make_settings(**values):
    return Settings(
        _env_file=None,
        SECRET_KEY="unit-test-signing-key",
        PROJECT_NAME="Tests",
        DATABASE_URL="postgresql://test:test@localhost/app_test",
        FIRST_SUPERUSER="admin@example.com",
        FIRST_SUPERUSER_PASSWORD="unit-test-password",
        **values,
    )


def test_app_setup_requires_all_fields():
    assert configured_app_fields({"TIKTOK_APP_ID": "app-test"}) == [
        "TIKTOK_APP_SECRET",
        "TIKTOK_REDIRECT_URI",
    ]
    assert configured_app_fields({"TIKTOK_APP_ID": " "}) == [
        "TIKTOK_APP_ID",
        "TIKTOK_APP_SECRET",
        "TIKTOK_REDIRECT_URI",
    ]


def test_absent_app_does_not_block_bootstrap():
    config = make_settings(
        TIKTOK_APP_ID="", TIKTOK_APP_SECRET="", TIKTOK_REDIRECT_URI=""
    )
    assert not config.tiktok_app_configured
    with pytest.raises(DomainError) as caught:
        config.require_tiktok_app()
    assert caught.value.code == "tiktok_app_not_configured"
    assert caught.value.retryable is False


def test_partial_app_names_all_missing_fields():
    config = make_settings(
        TIKTOK_APP_ID="app-test", TIKTOK_APP_SECRET="", TIKTOK_REDIRECT_URI=""
    )
    assert config.tiktok_app_missing_fields == [
        "TIKTOK_APP_SECRET",
        "TIKTOK_REDIRECT_URI",
    ]
    with pytest.raises(DomainError) as caught:
        config.require_tiktok_app()
    assert caught.value.code == "tiktok_app_incomplete"


def test_complete_app_is_configured():
    config = make_settings(
        TIKTOK_APP_ID="app-test",
        TIKTOK_APP_SECRET="opaque-app-secret",
        TIKTOK_REDIRECT_URI="https://example.com/api/integrations/tiktok/callback",
    )
    assert config.tiktok_app_configured
    config.require_tiktok_app()
    assert "opaque-app-secret" not in repr(config)


def test_sensitive_fields_hidden_from_repr():
    secret = "unique-sensitive-value"
    config = make_settings(
        TIKTOK_APP_SECRET=secret,
        CONNECTION_ENCRYPTION_KEY=secret,
        S3_ACCESS_KEY_ID=secret,
        S3_SECRET_ACCESS_KEY=secret,
    )
    assert secret not in repr(config)
    assert isinstance(config.TIKTOK_APP_SECRET, str)


def test_api_prefix_cannot_be_overridden():
    with pytest.raises(ValidationError):
        make_settings(API_V1_STR="/api/v1")


def test_feature_credentials_checked_when_feature_used():
    config = make_settings(
        CONNECTION_ENCRYPTION_KEY="",
        S3_BUCKET="",
        S3_ACCESS_KEY_ID="",
        S3_SECRET_ACCESS_KEY="",
    )
    with pytest.raises(DomainError):
        config.require_connection_encryption()
    with pytest.raises(DomainError):
        config.require_object_storage()
    config.CONNECTION_ENCRYPTION_KEY = Fernet.generate_key().decode()
    config.require_connection_encryption()
    config.CONNECTION_ENCRYPTION_KEY = "changethis"
    with pytest.raises(DomainError):
        config.require_connection_encryption()


def test_celery_routes_control_and_has_no_result_backend():
    from app.jobs.celery_app import celery_app

    assert celery_app.conf.task_acks_late
    assert celery_app.conf.task_reject_on_worker_lost
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.task_ignore_result
    assert celery_app.conf.result_backend is None
    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.accept_content == ["json"]
    job = celery_app.conf.beat_schedule["flush-dispatch"]
    assert job["task"] == "jobs.flush_dispatch"
    assert job["options"]["queue"] == "control"
    assert (
        celery_app.amqp.router.route({}, "jobs.flush_dispatch")["queue"].name
        == "control"
    )
    assert {queue.name for queue in celery_app.conf.task_queues} == {
        "resources",
        "builds",
        "control",
    }
