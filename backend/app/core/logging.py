from collections.abc import Mapping
from math import isfinite
from uuid import UUID

from app.core.errors import ERROR_HTTP_STATUS

_ID_FIELDS = {"tenant_id", "actor_id", "task_id", "step_id", "request_id"}


def log_fields(values: Mapping[str, object]) -> dict[str, str | int | float]:
    """Keep only bounded, typed operational metadata, never request/provider data."""
    result: dict[str, str | int | float] = {}
    for key, value in values.items():
        if key in _ID_FIELDS and isinstance(value, str | UUID):
            try:
                result[key] = str(UUID(str(value)))
            except ValueError:
                pass
        elif key == "error_code" and isinstance(value, str):
            if value in ERROR_HTTP_STATUS or value == "internal_error":
                result[key] = value
        elif (
            key == "duration_ms"
            and isinstance(value, int | float)
            and not isinstance(value, bool)
        ):
            if isfinite(value) and value >= 0:
                result[key] = value
    return result
