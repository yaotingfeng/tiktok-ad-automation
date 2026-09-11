import warnings
from collections.abc import Mapping
from typing import Any, Literal, Self
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from pydantic import (
    Field,
    HttpUrl,
    PostgresDsn,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.errors import ConfigurationError, DomainError
from app.core.usernames import Username

TIKTOK_APP_FIELDS = ("TIKTOK_APP_ID", "TIKTOK_APP_SECRET", "TIKTOK_REDIRECT_URI")


def configured_app_fields(values: Mapping[str, str | None]) -> list[str]:
    """Return missing field names only; never expose configuration values."""
    return [name for name in TIKTOK_APP_FIELDS if not (values.get(name) or "").strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Use top level .env file (one level above ./backend/)
        env_file="../.env",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )
    API_V1_STR: Literal["/api"] = "/api"
    SECRET_KEY: str = Field(repr=False)
    # 60 minutes * 24 hours * 8 days = 8 days
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 8
    FRONTEND_HOST: str = "http://localhost:5173"
    FASTAPI_ENV: Literal["development"] | None = None

    PROJECT_NAME: str
    SENTRY_DSN: HttpUrl | None = None
    DATABASE_URL: PostgresDsn = Field(repr=False)

    REDIS_URL: str = Field(default="redis://localhost:6379/0", repr=False)
    # Local broker publishing only; external API admission remains per request.
    DISPATCH_MAX_ROUNDS: int = Field(default=20, ge=1, le=100)
    DISPATCH_TIME_BUDGET_SECONDS: float = Field(default=1.0, gt=0, le=5)
    TIKTOK_APP_ID: str = ""
    TIKTOK_APP_SECRET: str = Field(default="", repr=False)
    TIKTOK_REDIRECT_URI: str = ""
    TIKTOK_AUTHORIZATION_URL: str = ""
    TIKTOK_CALL_POLICIES: dict[str, Any] = Field(default_factory=dict)
    # MCP 独立使用本环境注册材料；缺少 Marketing API 应用不影响该通道。
    MCP_CLIENT_REGISTRATION_REF: str = Field(default="", repr=False)
    MCP_REDIRECT_URI: str = ""
    # 仅部署方根据上游事实配置；缺省由所有 MCP 连接共用保守配额域。
    MCP_SERVICE_QUOTA_SCOPE: str | None = None
    CONNECTION_ENCRYPTION_KEY: str = Field(default="", repr=False)
    OBJECT_STORAGE_PROVIDER: Literal["s3", "r2"] = "s3"
    S3_ENDPOINT_URL: str = ""
    S3_BUCKET: str = ""
    S3_REGION: str = "us-east-1"
    S3_ACCESS_KEY_ID: str = Field(default="", repr=False)
    S3_SECRET_ACCESS_KEY: str = Field(default="", repr=False)
    # Engineering limits: the pinned official SDK buffers multipart files.
    MATERIAL_SDK_MAX_UPLOAD_BYTES: int = Field(default=256 * 1024 * 1024, gt=0)
    MATERIAL_SDK_UPLOAD_MAX_INFLIGHT: int = Field(default=1, gt=0)
    MATERIAL_INGEST_ENABLED: bool = False
    MATERIAL_CLEANUP_ENABLED: bool = False
    # R2 分片接收和 TikTok URL 转存支持 1 GiB；不放宽旧 SDK 整文件内存保护。
    MATERIAL_URL_MAX_UPLOAD_BYTES: int = Field(default=1024**3, gt=0)
    MATERIAL_STORAGE_GLOBAL_BYTES: int = Field(default=8 * 1024**3, gt=0)
    MATERIAL_STORAGE_TENANT_BYTES: int = Field(default=2 * 1024**3, gt=0)
    MATERIAL_PART_URL_SECONDS: int = Field(default=900, ge=60, le=900)
    MATERIAL_INGEST_URL_SECONDS: int = Field(default=7200, ge=60, le=7200)
    MATERIAL_VALIDATION_SECONDS: int = Field(default=300, ge=30, le=600)
    MATERIAL_ABANDON_SECONDS: int = Field(default=86400, ge=3600)
    # Exact platform media hosts must be verified for the deployment before relay.
    MATERIAL_REMOTE_MEDIA_HOSTS: frozenset[str] = Field(default_factory=frozenset)
    MATERIAL_ASSET_MAX_AGE_SECONDS: int = Field(default=900, gt=0)
    BC_CAPABILITY_MAX_AGE_SECONDS: int = Field(default=86400, ge=60, le=86400)
    # Engineering observation age for shared scene facts, not a platform quota.
    SCENE_MAX_AGE_SECONDS: int = Field(default=86400, ge=60, le=604800)

    @property
    def tiktok_app_missing_fields(self) -> list[str]:
        return configured_app_fields(
            {name: getattr(self, name) for name in TIKTOK_APP_FIELDS}
        )

    @property
    def tiktok_app_configured(self) -> bool:
        return not self.tiktok_app_missing_fields

    def require_tiktok_app(self) -> None:
        missing = self.tiktok_app_missing_fields
        if len(missing) == len(TIKTOK_APP_FIELDS):
            raise DomainError("tiktok_app_not_configured", "等待配置开发者应用")
        if missing:
            raise ConfigurationError("tiktok_app_incomplete", missing)

    def require_connection_encryption(self) -> None:
        try:
            Fernet(self.CONNECTION_ENCRYPTION_KEY.encode())
        except ValueError, TypeError:
            raise ConfigurationError(
                "connection_encryption_unconfigured", ["CONNECTION_ENCRYPTION_KEY"]
            ) from None

    def require_object_storage(self) -> None:
        names = ["S3_BUCKET", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY"]
        if self.OBJECT_STORAGE_PROVIDER == "s3":
            names.append("S3_REGION")
        placeholders = {
            "changethis",
            "change-me",
            "minioadmin",
            "example",
            "your-access-key",
            "your-secret-key",
        }
        missing = [
            name
            for name in names
            if not getattr(self, name).strip()
            or getattr(self, name).lower() in placeholders
        ]
        if self.OBJECT_STORAGE_PROVIDER == "r2":
            try:
                endpoint = urlsplit(self.S3_ENDPOINT_URL)
                valid_endpoint = (
                    endpoint.scheme == "https"
                    and endpoint.hostname is not None
                    and endpoint.hostname.endswith(".r2.cloudflarestorage.com")
                    and endpoint.hostname != "r2.cloudflarestorage.com"
                    and endpoint.username is None
                    and endpoint.password is None
                    and endpoint.port in {None, 443}
                    and endpoint.path in {"", "/"}
                    and not endpoint.query
                    and not endpoint.fragment
                )
            except ValueError:
                valid_endpoint = False
            if not valid_endpoint:
                missing.append("S3_ENDPOINT_URL")
        if missing:
            raise ConfigurationError("object_storage_unconfigured", missing)

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def _use_psycopg_driver(cls, value: str | PostgresDsn) -> str:
        database_url = str(value)
        for scheme in ("postgres://", "postgresql://"):
            if database_url.startswith(scheme):
                return database_url.replace(scheme, "postgresql+psycopg://", 1)
        return database_url

    TEST_USERNAME: Username = "test-user"
    FIRST_SUPERUSER: Username
    FIRST_SUPERUSER_PASSWORD: str = Field(repr=False)

    def _check_default_secret(self, var_name: str, value: str | None) -> None:
        if value == "changethis":
            message = (
                f'The value of {var_name} is "changethis", '
                "for security, please change it, at least for deployments."
            )
            if self.FASTAPI_ENV == "development":
                warnings.warn(message, stacklevel=1)
            else:
                raise ValueError(message)

    @model_validator(mode="after")
    def _enforce_non_default_secrets(self) -> Self:
        self._check_default_secret("SECRET_KEY", self.SECRET_KEY)
        for host in self.DATABASE_URL.hosts():
            self._check_default_secret("DATABASE_URL password", host["password"])
        self._check_default_secret(
            "FIRST_SUPERUSER_PASSWORD", self.FIRST_SUPERUSER_PASSWORD
        )

        return self


settings = Settings()  # type: ignore # ty: ignore[unused-ignore-comment]
