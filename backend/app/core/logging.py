from math import isfinite
from uuid import UUID

from app.core.errors import ERROR_HTTP_STATUS

_ID_FIELDS = {"tenant_id", "actor_id", "task_id", "step_id", "request_id"}


def log_fields(values: dict) -> dict:
    """Keep only bounded, typed operational metadata, never request/provider data."""
    result = {}
    for key, value in values.items():
        if key in _ID_FIELDS and isinstance(value, str | UUID):
            try:
                result[key] = str(UUID(str(value)))
            except ValueError:
                pass
        elif key == "error_code" and isinstance(value, str):
            if value in ERROR_HTTP_STATUS or value == "internal_error":
                result[key] = value
        elif key == "duration_ms" and type(value) in (int, float):
            if isfinite(value) and value >= 0:
                result[key] = value
    return result
