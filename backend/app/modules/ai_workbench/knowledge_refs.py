"""Versioned, sanitized knowledge references used by DSH review evidence.

The review loop stores only references and public metadata.  Credentials and model
internal reasoning are deliberately removed before any JSON is persisted or
returned to an API caller.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_SENSITIVE = {
    "token", "access_token", "refresh_token", "password", "secret", "credential",
    "cookie", "authorization", "api_key", "apikey", "private_key", "chain_of_thought",
    "reasoning", "thoughts", "model_trace", "internal_reasoning", "prompt_tokens",
    "completion_tokens",
}


def sanitize_public(value: Any) -> Any:
    """Return JSON-safe evidence with secret and model-trace fields removed."""
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            # Match secret field names exactly (or explicit suffixes).  Safe
            # optimizer fields such as ``authorization_limits`` must remain
            # available to reviewers.
            if lowered in _SENSITIVE or any(lowered.endswith(suffix) for suffix in ("_token", "_secret", "_password", "_credential", "_trace")):
                continue
            clean[key_text] = sanitize_public(item)
        return clean
    if isinstance(value, (list, tuple, set)):
        return [sanitize_public(item) for item in value]
    if hasattr(value, "model_dump"):
        return sanitize_public(value.model_dump(mode="json"))
    if hasattr(value, "isoformat") and not isinstance(value, (str, bytes)):
        return value.isoformat()
    return value


def versioned_reference(*, key: str, version: str | int, source: str, metadata: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build the stable public shape used inside frozen review evidence."""
    return {
        "key": key,
        "version": str(version),
        "source": source,
        "metadata": sanitize_public(dict(metadata or {})),
    }


def snapshot_knowledge_refs(refs: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] | None) -> list[dict[str, Any]]:
    """Freeze caller-provided knowledge references without exposing private data."""
    result: list[dict[str, Any]] = []
    for ref in refs or ():
        if not ref.get("key") or ref.get("version") is None:
            continue
        result.append(versioned_reference(
            key=str(ref["key"]),
            version=ref["version"],
            source=str(ref.get("source", "DSH")),
            metadata=ref.get("metadata") if isinstance(ref.get("metadata"), Mapping) else {},
        ))
    return result
