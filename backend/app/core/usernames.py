"""Canonical platform account identifiers, independent of provider credentials."""

from typing import Annotated, Any

from pydantic import BeforeValidator, StringConstraints


def normalize_username(value: Any) -> Any:
    return value.strip().lower() if isinstance(value, str) else value


Username = Annotated[
    str,
    BeforeValidator(normalize_username),
    StringConstraints(
        strict=True, min_length=3, max_length=64, pattern=r"^[a-z0-9_.-]+$"
    ),
]
