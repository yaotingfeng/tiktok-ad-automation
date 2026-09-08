from fastapi import Request
from fastapi.responses import JSONResponse

# Register exact business codes here; never infer status from provider text.
ERROR_HTTP_STATUS: dict[str, int] = {
    "strategy_not_found": 404,
    "copy_pool_not_found": 404,
    "copy_pool_exhausted": 422,
    "invalid_group_config": 422,
    "invalid_name_template": 422,
    "name_too_long": 422,
    "invalid_strategy_name": 422,
    "invalid_cta_options": 422,
    "empty_title": 422,
    "invalid_title": 422,
    "request_id_conflict": 409,
    "candidate_not_available": 409,
    "invalid_page_size": 422,
    "provider_worker_unbounded": 503,
    "provider_request_invalid": 422,
    "provider_schema_unsupported": 409,
    "provider_session_expired": 409,
    "provider_application_forbidden": 403,
    "provider_rejected": 409,
    "provider_unavailable": 503,
    "provider_result_unknown": 409,
    "provider_application_discovery_unverified": 409,
    "provider_channel_prefix_missing": 409,
    "provider_verification_in_progress": 409,
    "lookup_incomplete": 409,
    "attribution_contract_unverified": 409,
    "config_unverifiable": 409,
    "config_conflict": 409,
    "invalid_account_action": 422,
    "invalid_cursor": 422,
    "invalid_resolve_request": 422,
    "discovery_failed": 503,
    "account_not_in_bc": 404,
    "account_ownership_conflict": 409,
    "account_metadata_incomplete": 409,
    "account_access_denied": 403,
    "no_upload_account": 409,
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
    "app_not_configured": 503,
    "invalid_authorization_url": 422,
    "invalid_oauth_state": 409,
    "invalid_auth_code": 422,
    "invalid_token_response": 409,
    "oauth_result_unknown": 409,
    "connection_not_found": 404,
    "connection_unavailable": 409,
    "credential_invalid": 409,
    "credential_tenant_mismatch": 403,
    "tiktok_response_error": 409,
    "admission_deferred": 429,
}

# Public messages are application-owned. DomainError.message may contain raw
# integration details, so it must never become an HTTP response or log field.
ERROR_PUBLIC_MESSAGES: dict[str, str] = {
    "provider_session_expired": "版权方登录已失效，请重新验证当前连接",
    "provider_application_forbidden": "当前版权方连接没有该应用权限",
    "provider_result_unknown": "版权方写入结果未知，需要回查后继续",
    "provider_channel_prefix_missing": "未发现当前应用的渠道前缀",
    "provider_verification_in_progress": "当前版权方连接正在验证",
    "lookup_incomplete": "尚未核实完整链接历史，不能确认链接不存在",
    "attribution_contract_unverified": "版权方归因契约尚未核实",
    "config_unverifiable": "已有链接配置尚不能核实",
    "config_conflict": "已有链接配置与本次请求冲突，不能覆盖",

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
    "app_not_configured": "等待配置开发者应用授权地址",
    "invalid_oauth_state": "授权回调已失效或已使用，请重新发起授权",
    "oauth_result_unknown": "授权结果未知，请重新发起授权",
    "admission_deferred": "调用额度暂不可用，请稍后重试",
}
_STATUS_MESSAGES = {
    403: "当前操作无权限",
    404: "资源不存在或不可见",
    409: "资源版本或请求标识冲突",
    422: "配置或输入无效",
    429: "调用额度暂不可用",
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
    retry_after_ms = getattr(exc, "retry_after_ms", None)
    headers = (
        {"Retry-After": str(max(1, (retry_after_ms + 999) // 1000))}
        if isinstance(retry_after_ms, int) and retry_after_ms > 0
        else None
    )
    return JSONResponse(
        headers=headers,
        status_code=status,
        content={
            "code": code,
            "message": message,
            "retryable": exc.retryable,
        },
    )
