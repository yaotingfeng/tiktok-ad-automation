"""SDK 素材唯一传输入口；每次真实请求共享工厂授权、配额与绝对期限。"""

import json
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

import business_api_client.tiktok_business.tiktok_exceptions as sdk_errors  # type: ignore[import-untyped]
from business_api_client.api.file_api import FileApi  # type: ignore[import-untyped]
from business_api_client.models.filtering_video_ad_search import (  # type: ignore[import-untyped]
    FilteringVideoAdSearch,
)
from business_api_client.rest import ApiException  # type: ignore[import-untyped]
from urllib3.exceptions import HTTPError

from app.core.config import settings
from app.core.errors import DomainError
from app.integrations.tiktok.contracts import materials as contracts
from app.integrations.tiktok.contracts.common import (
    CallEvidence,
    McpBusinessResponse,
    RemoteCallError,
)
from app.integrations.tiktok.official.accounts import RequestScope, _strict_sdk_envelope
from app.integrations.tiktok.sdk import SDK_SCOPE_INTERRUPTS, AccountAdmissionDeferred
from app.modules.materials import cover_sdk, sdk_assets


def _unique_upload_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError("ambiguous upload receipt")
        fields[key] = value
    return fields


def _reject_json_constant(_value: str) -> None:
    raise ValueError("invalid JSON constant")


class SDKMaterialOperations(sdk_assets.MaterialReadAdapter):
    def __init__(
        self,
        client: Any,
        *,
        request_scope: RequestScope,
        deadline: datetime,
        preview_allowed_hosts: frozenset[str] = frozenset(),
        api_scope_ids: frozenset[int] | None = None,
    ):
        super().__init__(preview_allowed_hosts=preview_allowed_hosts)
        self._client = client
        self._scope = request_scope
        self._deadline = deadline
        self._api_scope_ids = api_scope_ids

    def _call(
        self,
        operation: str,
        advertiser_id: str,
        arguments: dict[str, Any],
        budget: contracts.RemoteCallBudget,
    ) -> McpBusinessResponse:
        if budget.deadline != self._deadline:
            raise DomainError("material_deadline", "素材预算与本次会话期限不一致")
        endpoint = {
            "materials.get_images": cover_sdk.INFO_ENDPOINT,
            "materials.search_images": cover_sdk.SEARCH_ENDPOINT,
            "materials.get_suggested_covers": cover_sdk.SUGGEST_ENDPOINT,
        }.get(operation)
        if endpoint:
            cover_sdk.require_cover_scopes(self._api_scope_ids, endpoint=endpoint)
        # 不再调用旧 App-ID 准入；工厂的 request_scope 是唯一物理请求授权与配额边界。
        with self._scope(advertiser_id, operation, budget.deadline):
            budget.timeout(upload=False)
            try:
                with _strict_sdk_envelope(self._client):
                    if operation == "materials.get_videos":
                        return _read_video_response(
                            self._client,
                            advertiser_id=advertiser_id,
                            video_id=arguments["video_ids"][0],
                            budget=budget,
                        )
                    if operation == "materials.search_videos":
                        return _search_videos_response(
                            self._client,
                            advertiser_id=advertiser_id,
                            page=arguments["page"],
                            material_ids=arguments.get("filtering", {}).get(
                                "material_ids"
                            ),
                            budget=budget,
                        )
                    if operation == "materials.get_images":
                        return _read_image_response(
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
                    return _call_response(
                        self._client, path, query=arguments, budget=budget
                    )
            except ApiException, sdk_errors.TiktokSDKError, HTTPError:
                raise RemoteCallError(
                    "tiktok_response_error", effect="UNKNOWN", evidence=CallEvidence()
                ) from None

    def _upload_video(
        self,
        request: contracts.URLVideoUpload | contracts.FileVideoUpload,
        *,
        budget: contracts.RemoteCallBudget,
    ) -> contracts.VideoReceipt:
        sent = False
        evidence = CallEvidence()
        try:
            if budget.deadline != self._deadline:
                raise DomainError("material_deadline", "素材预算与本次会话期限不一致")
            digest = sdk_assets.validate_video_upload(request)
            is_url = isinstance(request, contracts.URLVideoUpload)
            maximum = (
                settings.MATERIAL_URL_MAX_UPLOAD_BYTES
                if is_url
                else settings.MATERIAL_SDK_MAX_UPLOAD_BYTES
            )
            if (
                type(request.byte_size) is not int
                or not 0 < request.byte_size <= maximum
            ):
                raise DomainError(
                    "material_channel_capacity"
                    if is_url
                    else "sdk_upload_capacity_exceeded",
                    "文件超过当前上传容量",
                )
            operation = (
                "materials.upload_video_url"
                if is_url
                else "materials.upload_video_file"
            )
            # API延续既有应用容量与显式参数；不把它们声称为MCP服务能力证明。
            arguments = (
                {"video_url": request.url}
                if isinstance(request, contracts.URLVideoUpload)
                else {"video_file": request.local_path}
            )
            with self._scope(request.advertiser_id, operation, budget.deadline):
                timeout = budget.timeout(upload=True)
                sent = True
                FileApi(self._client).ad_video_upload(
                    access_token=self._client.default_headers["Access-Token"],
                    advertiser_id=request.advertiser_id,
                    upload_type="UPLOAD_BY_URL" if is_url else "UPLOAD_BY_FILE",
                    file_name=request.file_name,
                    video_signature=digest,
                    auto_bind_enabled=False,
                    auto_fix_enabled=False,
                    async_req=True,
                    _request_timeout=timeout,
                    **arguments,
                ).get()
                # async官方入口保留完整原始envelope；严格code后只读取真实回执ID。
                response = _upload_response(self._client, array=True)
                return sdk_assets.video_upload_receipt(
                    response,
                    advertiser_id=request.advertiser_id,
                    channel="OFFICIAL_API",
                )
        except SDK_SCOPE_INTERRUPTS:
            raise
        except RemoteCallError, AccountAdmissionDeferred:
            raise
        except Exception as error:
            code = (
                error.code
                if isinstance(error, DomainError) and not sent
                else "material_response_unknown"
            )
            raise RemoteCallError(
                code, effect="UNKNOWN" if sent else "NOT_SENT", evidence=evidence
            ) from None

    def upload_video_url(
        self, request: contracts.URLVideoUpload, *, budget: contracts.RemoteCallBudget
    ) -> contracts.VideoReceipt:
        return self._upload_video(request, budget=budget)

    def upload_video_file(
        self, request: contracts.FileVideoUpload, *, budget: contracts.RemoteCallBudget
    ) -> contracts.VideoReceipt:
        return self._upload_video(request, budget=budget)

    def read_video_cover(
        self,
        *,
        advertiser_id: str,
        video_id: str,
        md5: str,
        budget: contracts.RemoteCallBudget,
    ) -> contracts.VideoCover:
        cover_sdk.require_cover_scopes(
            self._api_scope_ids, endpoint=cover_sdk.VIDEO_INFO_ENDPOINT
        )
        return super().read_video_cover(
            advertiser_id=advertiser_id, video_id=video_id, md5=md5, budget=budget
        )

    def upload_image_url(
        self, request: contracts.URLImageUpload, *, budget: contracts.RemoteCallBudget
    ) -> contracts.ImageReceipt:
        sent = False
        try:
            if budget.deadline != self._deadline:
                raise DomainError("material_deadline", "素材预算与本次会话期限不一致")
            cover_sdk.require_cover_scopes(
                self._api_scope_ids, endpoint=cover_sdk.UPLOAD_ENDPOINT
            )
            cover_sdk.validate_image_upload(request)
            with self._scope(
                request.advertiser_id, "materials.upload_image_url", budget.deadline
            ):
                budget.timeout(upload=True)
                sent = True
                _call_response(
                    self._client,
                    cover_sdk.UPLOAD_ENDPOINT,
                    body={
                        "advertiser_id": request.advertiser_id,
                        "upload_type": "UPLOAD_BY_URL",
                        "image_url": request.url,
                        "file_name": request.file_name,
                    },
                    budget=budget,
                )
                response = _upload_response(self._client, array=False)
                return cover_sdk.image_receipt(
                    response, advertiser_id=request.advertiser_id
                )
        except SDK_SCOPE_INTERRUPTS:
            raise
        except RemoteCallError, AccountAdmissionDeferred:
            raise
        except Exception as error:
            raise RemoteCallError(
                error.code
                if isinstance(error, DomainError) and not sent
                else "cover_response_error",
                effect="UNKNOWN" if sent else "NOT_SENT",
                evidence=CallEvidence(),
            ) from None


def _read_video_response(
    client: Any,
    *,
    advertiser_id: str,
    video_id: str,
    budget: contracts.RemoteCallBudget,
) -> McpBusinessResponse:
    return sdk_assets._response(
        FileApi(client).ad_video_info(
            advertiser_id=advertiser_id,
            video_ids=[video_id],
            access_token=client.default_headers["Access-Token"],
            _request_timeout=budget.timeout(upload=False),
        )
    )


def _search_videos_response(
    client: Any,
    *,
    advertiser_id: str,
    page: int,
    material_ids: list[str] | None = None,
    budget: contracts.RemoteCallBudget,
) -> McpBusinessResponse:
    kwargs = {}
    if material_ids:
        kwargs["filtering"] = FilteringVideoAdSearch(material_ids=material_ids)
    return sdk_assets._response(
        FileApi(client).ad_video_search(
            advertiser_id=advertiser_id,
            access_token=client.default_headers["Access-Token"],
            page=page,
            page_size=sdk_assets.PAGE_SIZE,
            _request_timeout=budget.timeout(upload=False),
            **kwargs,
        )
    )


def _upload_response(client: Any, *, array: bool) -> McpBusinessResponse:
    payload = client.last_response.data
    if len(payload) > 8 * 1024 * 1024:
        raise ValueError("oversized upload receipt")
    raw = json.loads(
        payload,
        object_pairs_hook=_unique_upload_fields,
        parse_constant=_reject_json_constant,
        parse_float=Decimal,
    )
    request_id = raw.get("request_id") if type(raw) is dict else None
    evidence = CallEvidence(
        request_id=request_id
        if isinstance(request_id, str)
        and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id)
        else None
    )
    if (
        type(raw) is not dict
        or type(raw.get("code")) is not int
        or raw["code"] != 0
        or type(raw.get("data")) is not (list if array else dict)
    ):
        raise RemoteCallError(
            "material_response_unknown", effect="UNKNOWN", evidence=evidence
        )
    return McpBusinessResponse(raw["data"], evidence)


def _call_response(
    client: Any,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    budget: contracts.RemoteCallBudget,
) -> McpBusinessResponse:
    method = "POST" if body is not None else "GET"
    headers = {
        "Access-Token": client.default_headers.get("Access-Token", ""),
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"
    future = client.call_api(
        path,
        method,
        {},
        list((query or {}).items()),
        headers,
        body=body,
        response_type="InlineResponse200",
        auth_settings=[],
        _return_http_data_only=True,
        async_req=True,
        _request_timeout=budget.timeout(upload=body is not None),
    )
    return sdk_assets._response(future.get())


def _read_image_response(
    client: Any,
    *,
    advertiser_id: str,
    image_id: str,
    budget: contracts.RemoteCallBudget,
) -> McpBusinessResponse:
    return sdk_assets._response(
        FileApi(client).file_image_ad_info(
            advertiser_id=advertiser_id,
            image_ids=[image_id],
            access_token=client.default_headers.get("Access-Token", ""),
            _request_timeout=budget.timeout(upload=False),
        )
    )
