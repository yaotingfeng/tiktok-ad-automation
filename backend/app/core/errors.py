from fastapi import Request
from fastapi.responses import JSONResponse

# Register exact business codes here; never infer status from provider text.
ERROR_HTTP_STATUS: dict[str, int] = {
    "tenant_forbidden": 403,
    "action_forbidden": 403,
    "platform_forbidden": 403,
    "invalid_tenant": 422,
    "invalid_member": 422,
    "last_tenant_admin": 409,
    "unknown_action": 422,
    "permission_denied": 403,
    "public_signup_disabled": 403,
    "resource_not_found": 404,
    "tenant_not_found": 404,
    "version_conflict": 409,
    "idempotency_conflict": 409,
    "dispatch_key_conflict": 409,
    "dispatch_task_not_allowed": 422,
    "dispatch_payload_invalid": 422,
    "configuration_invalid": 422,
    "admission_unconfigured": 503,
    "admission_policy_invalid": 503,
    "tiktok_app_incomplete": 422,
    "connection_encryption_unconfigured": 422,
    "object_storage_unconfigured": 422,
    "admission_unavailable": 503,
    "external_unavailable": 503,
    "tiktok_app_not_configured": 503,
    "tiktok_oauth_unavailable": 503,
}

# Public messages are application-owned. DomainError.message may contain raw
# integration details, so it must never become an HTTP response or log field.
ERROR_PUBLIC_MESSAGES: dict[str, str] = {
    "last_tenant_admin": "请先设置其他租户管理员",
    "invalid_tenant": "租户名称与初始管理员必须有效",
    "invalid_member": "成员和租户角色必须有效",
    "public_signup_disabled": "公开注册已关闭，请联系平台管理员",
    "tiktok_app_not_configured": "等待配置开发者应用",
    "tiktok_app_incomplete": "开发者应用配置不完整",
    "tiktok_oauth_unavailable": "授权接入尚未开放",
    "admission_unconfigured": "请配置应用调用额度",
    "admission_policy_invalid": "调用额度配置无效",
    "admission_unavailable": "调用配额服务暂不可用",
    "dispatch_key_conflict": "同一任务标识的配置不一致",
}
_STATUS_MESSAGES = {
    403: "当前操作无权限",
    404: "资源不存在或不可见",
    409: "资源版本或请求标识冲突",
    422: "配置或输入无效",
    503: "服务暂不可用",
    500: "内部服务错误",
}


class DomainError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class ConfigurationError(DomainError):
    """Expose only predefined setting names, never the values that failed checks."""

    ALLOWED_FIELDS = frozenset(
        {
            "TIKTOK_APP_ID",
            "TIKTOK_APP_SECRET",
            "TIKTOK_REDIRECT_URI",
            "CONNECTION_ENCRYPTION_KEY",
            "S3_BUCKET",
            "S3_REGION",
            "S3_ACCESS_KEY_ID",
            "S3_SECRET_ACCESS_KEY",
        }
    )

    def __init__(self, code: str, fields: list[str]):
        super().__init__(code, "Configuration incomplete")
        self.fields = tuple(name for name in fields if name in self.ALLOWED_FIELDS)


async def domain_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, DomainError):
        return JSONResponse(
            status_code=500,
            content={
                "code": "internal_error",
                "message": _STATUS_MESSAGES[500],
                "retryable": False,
            },
        )
    status = ERROR_HTTP_STATUS.get(exc.code, 500)
    code = exc.code if exc.code in ERROR_HTTP_STATUS else "internal_error"
    message = ERROR_PUBLIC_MESSAGES.get(code, _STATUS_MESSAGES[status])
    if isinstance(exc, ConfigurationError) and exc.fields:
        message += "，缺少或无效：" + ", ".join(exc.fields)
    return JSONResponse(
        status_code=status,
        content={
            "code": code,
            "message": message,
            "retryable": exc.retryable,
        },
    )
