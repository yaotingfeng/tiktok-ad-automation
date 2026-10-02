"""Provider kind metadata and the runtime adapter dispatch boundary.

The connection layer must validate credentials from one canonical table.  New
providers are intentionally allowed to be saved before their protocol adapter
is deployed; verification then fails closed instead of accidentally using the
Wangyan protocol.
"""

from collections.abc import Mapping

import httpx

from .contract import ProviderClient, failure
from .jiashu import JiashuClient
from .wangyan import WangyanClient

_CREDENTIAL_FIELDS: Mapping[str, frozenset[str]] = {
    "wangyan": frozenset({"email", "password"}),
    "jiashu": frozenset({"username", "password"}),
    "duiba": frozenset({"account", "password"}),
    "gangganhao": frozenset({"portal_id", "username", "password"}),
    "rongliang": frozenset({"email", "password"}),
}


def credential_fields(kind: str) -> frozenset[str]:
    """Return the exact encrypted credential keys accepted for *kind*."""

    try:
        return _CREDENTIAL_FIELDS[kind]
    except KeyError:
        raise failure("provider_request_invalid") from None


def adapter_for_kind(
    kind: str,
    http: httpx.Client,
    credentials: dict[str, str],
    *,
    application_id: str = "",
    channel_prefix: str = "",
) -> ProviderClient:
    """Build a session adapter without falling back across provider kinds."""

    if kind == "jiashu":
        return JiashuClient(
            http,
            session=credentials["session"],
            application_id=application_id,
            channel_prefix=channel_prefix,
        )
    if kind == "wangyan":
        return WangyanClient(
            http, token=credentials["token"], application_id=application_id
        )
    raise failure("provider_unavailable", retryable=True)
