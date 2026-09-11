"""SDK 素材读适配；Task 3/4 删除 legacy raw 入口时一并迁移唯一传输原语。"""

from datetime import datetime
from typing import Any

import business_api_client.tiktok_business.tiktok_exceptions as sdk_errors  # type: ignore[import-untyped]
from business_api_client.rest import ApiException  # type: ignore[import-untyped]
from urllib3.exceptions import HTTPError

from app.core.errors import DomainError
from app.integrations.tiktok.contracts import materials as contracts
from app.integrations.tiktok.contracts.common import (
    CallEvidence,
    McpBusinessResponse,
    RemoteCallError,
)
from app.integrations.tiktok.official.accounts import RequestScope, _strict_sdk_envelope
from app.modules.materials import cover_sdk, sdk_assets


class SDKMaterialOperations(sdk_assets.MaterialReadAdapter):
    def __init__(
        self,
        client: Any,
        *,
        request_scope: RequestScope,
        deadline: datetime,
        preview_allowed_hosts: frozenset[str] = frozenset(),
    ):
        super().__init__(preview_allowed_hosts=preview_allowed_hosts)
        self._client = client
        self._scope = request_scope
        self._deadline = deadline

    def _call(
        self,
        operation: str,
        advertiser_id: str,
        arguments: dict[str, Any],
        budget: contracts.RemoteCallBudget,
    ) -> McpBusinessResponse:
        if budget.deadline != self._deadline:
            raise DomainError("material_deadline", "素材预算与本次会话期限不一致")
        # 不再调用旧 App-ID 准入；工厂的 request_scope 是唯一物理请求授权与配额边界。
        with self._scope(advertiser_id, operation, budget.deadline):
            budget.timeout(upload=False)
            try:
                with _strict_sdk_envelope(self._client):
                    if operation == "materials.get_videos":
                        return sdk_assets._read_video_response(
                            self._client,
                            advertiser_id=advertiser_id,
                            video_id=arguments["video_ids"][0],
                            budget=budget,
                        )
                    if operation == "materials.search_videos":
                        return sdk_assets._search_videos_response(
                            self._client,
                            advertiser_id=advertiser_id,
                            page=arguments["page"],
                            material_ids=arguments.get("filtering", {}).get(
                                "material_ids"
                            ),
                            budget=budget,
                        )
                    if operation == "materials.get_images":
                        return cover_sdk._read_image_response(
                            self._client,
                            advertiser_id=advertiser_id,
                            image_id=arguments["image_ids"][0],
                            budget=budget,
                        )
                    path = {
                        "materials.get_suggested_covers": cover_sdk.SUGGEST_ENDPOINT,
                        "materials.search_images": cover_sdk.SEARCH_ENDPOINT,
                    }.get(operation)
                    if path is None:
                        raise DomainError(
                            "material_request_invalid", "素材读取操作无效"
                        )
                    return cover_sdk._call_response(
                        self._client, path, query=arguments, budget=budget
                    )
            except ApiException, sdk_errors.TiktokSDKError, HTTPError:
                raise RemoteCallError(
                    "tiktok_response_error", effect="UNKNOWN", evidence=CallEvidence()
                ) from None

    def upload_video_url(
        self, request: contracts.URLVideoUpload, *, budget: contracts.RemoteCallBudget
    ) -> contracts.VideoReceipt:
        # 新 gateway 写入在 Task 3 接入持久 REQUEST_ARMED/回执后开放；现有写任务仍走唯一旧入口。
        raise RemoteCallError(
            "material_channel_unverified", effect="NOT_SENT", evidence=CallEvidence()
        )

    def upload_video_file(
        self, request: contracts.FileVideoUpload, *, budget: contracts.RemoteCallBudget
    ) -> contracts.VideoReceipt:
        raise RemoteCallError(
            "material_channel_unverified", effect="NOT_SENT", evidence=CallEvidence()
        )

    def upload_image_url(
        self, request: contracts.URLImageUpload, *, budget: contracts.RemoteCallBudget
    ) -> contracts.ImageReceipt:
        raise RemoteCallError(
            "material_channel_unverified", effect="NOT_SENT", evidence=CallEvidence()
        )
