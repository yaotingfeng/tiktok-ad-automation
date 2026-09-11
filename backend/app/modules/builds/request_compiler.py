"""纯字段编解码；不读时钟，不填授权/素材，不修改已冻结请求。"""

from typing import Literal

from pydantic import Field, ValidationError

from app.integrations.tiktok.contracts.builds import (
    AdCreate,
    AdGroupCreate,
    BuildReadQuery,
    CampaignCreate,
    CreateIntent,
    CreativeAsset,
    CtaAsset,
    CtaCreate,
    FrozenModel,
    Id,
)
from app.integrations.tiktok.contracts.common import CallEvidence, RemoteCallError


class _CampaignBody(CampaignCreate):
    name: Id = Field(alias="campaign_name")


class _Targeting(FrozenModel):
    location_ids: tuple[Id, ...] = Field(min_length=1)


class _AdGroupBody(AdGroupCreate):
    name: Id = Field(alias="adgroup_name")


class _Video(FrozenModel):
    video_id: Id


class _Image(FrozenModel):
    web_uri: Id


class _CreativeInfo(FrozenModel):
    identity_type: Literal["BC_AUTH_TT"]
    identity_id: Id
    identity_authorized_bc_id: Id
    ad_format: Literal["SINGLE_VIDEO"]
    video_info: _Video
    image_info: tuple[_Image, ...] = Field(min_length=1, max_length=1)


class _Creative(FrozenModel):
    creative_info: _CreativeInfo


class _Text(FrozenModel):
    ad_text: Id = Field(max_length=100)


class _Url(FrozenModel):
    landing_page_url: Id


class _AdConfiguration(FrozenModel):
    call_to_action_id: Id


class _AdBody(FrozenModel):
    advertiser_id: Id
    adgroup_id: Id
    ad_name: Id
    operation_status: Literal["ENABLE"]
    creative_list: tuple[_Creative, ...] = Field(min_length=1, max_length=50)
    ad_text_list: tuple[_Text, ...] = Field(min_length=1, max_length=1)
    landing_page_url_list: tuple[_Url, ...] = Field(min_length=1, max_length=1)
    ad_configuration: _AdConfiguration


class _CtaBody(FrozenModel):
    advertiser_id: Id
    creative_portfolio_type: Literal["CTA"]
    portfolio_content: tuple[CtaAsset, ...] = Field(min_length=1, max_length=50)


def _invalid(field: str, value: object) -> ValidationError:
    return ValidationError.from_exception_data(
        "CreateIntent",
        [
            {
                "type": "value_error",
                "loc": (field,),
                "input": value,
                "ctx": {"error": ValueError("invalid frozen request field")},
            }
        ],
    )


def decode_intent(kind: str, body: dict[str, object]) -> CreateIntent:
    """仅解码本地历史/待冻结输入；旧 JSON 浮点兼容不构成远端精度证据。"""
    if not isinstance(body, dict) or "kind" in body:
        raise _invalid("body", body)
    values = dict(body)
    # 兼容旧冻结 JSON 数值表示，只在输入边界十进制化，不改原记录。
    for field in ("budget", "roas_bid"):
        if type(values.get(field)) is float:
            values[field] = str(values[field])
    return decode_observed_intent(kind, values)


def decode_observed_intent(kind: str, body: dict[str, object]) -> CreateIntent:
    """远端只接受精确金额类型；SDK 浮点可能已舍入，不能转回文本冒充精确事实。"""
    if not isinstance(body, dict) or "kind" in body:
        raise _invalid("body", body)
    values = dict(body)
    if kind == "CAMPAIGN":
        parsed = _CampaignBody.model_validate(values)
        return CampaignCreate.model_validate(parsed.model_dump())
    if kind == "ADGROUP":
        if "location_ids" in values:
            raise _invalid("location_ids", values["location_ids"])
        targeting = _Targeting.model_validate(values.pop("targeting_spec", None))
        values["location_ids"] = targeting.location_ids
        parsed_group = _AdGroupBody.model_validate(values)
        return AdGroupCreate.model_validate(parsed_group.model_dump())
    if kind == "AD":
        ad = _AdBody.model_validate(values)
        info = ad.creative_list[0].creative_info
        identity = (
            info.identity_type,
            info.identity_id,
            info.identity_authorized_bc_id,
        )
        if any(
            (
                item.creative_info.identity_type,
                item.creative_info.identity_id,
                item.creative_info.identity_authorized_bc_id,
            )
            != identity
            for item in ad.creative_list
        ):
            raise _invalid("creative_list.identity", values["creative_list"])
        return AdCreate(
            advertiser_id=ad.advertiser_id,
            adgroup_id=ad.adgroup_id,
            name=ad.ad_name,
            operation_status=ad.operation_status,
            identity_type=info.identity_type,
            identity_id=info.identity_id,
            identity_authorized_bc_id=info.identity_authorized_bc_id,
            text=ad.ad_text_list[0].ad_text,
            landing_page_url=ad.landing_page_url_list[0].landing_page_url,
            portfolio_id=ad.ad_configuration.call_to_action_id,
            assets=tuple(
                CreativeAsset(
                    video_id=item.creative_info.video_info.video_id,
                    image_id=item.creative_info.image_info[0].web_uri,
                )
                for item in ad.creative_list
            ),
        )
    if kind == "CTA":
        cta = _CtaBody.model_validate(values)
        return CtaCreate(advertiser_id=cta.advertiser_id, assets=cta.portfolio_content)
    raise _invalid("kind", kind)


def encode_intent(intent: CreateIntent) -> dict[str, object]:
    """持久请求 JSON 使用精确十进制字符串；发送适配器另行处理 wire 数字。"""
    body = intent.model_dump(mode="json", exclude={"kind"})
    if isinstance(intent, CampaignCreate):
        body["campaign_name"] = body.pop("name")
    elif isinstance(intent, AdGroupCreate):
        body["adgroup_name"] = body.pop("name")
        body["targeting_spec"] = {"location_ids": body.pop("location_ids")}
    elif isinstance(intent, AdCreate):
        return {
            "advertiser_id": intent.advertiser_id,
            "adgroup_id": intent.adgroup_id,
            "ad_name": intent.name,
            "operation_status": intent.operation_status,
            "creative_list": [
                {
                    "creative_info": {
                        "identity_type": intent.identity_type,
                        "identity_id": intent.identity_id,
                        "identity_authorized_bc_id": intent.identity_authorized_bc_id,
                        "ad_format": "SINGLE_VIDEO",
                        "video_info": {"video_id": asset.video_id},
                        "image_info": [{"web_uri": asset.image_id}],
                    }
                }
                for asset in intent.assets
            ],
            "ad_text_list": [{"ad_text": intent.text}],
            "landing_page_url_list": [{"landing_page_url": intent.landing_page_url}],
            "ad_configuration": {"call_to_action_id": intent.portfolio_id},
        }
    elif isinstance(intent, CtaCreate):
        return {
            "advertiser_id": intent.advertiser_id,
            "creative_portfolio_type": "CTA",
            "portfolio_content": body["assets"],
        }
    return body


READ_OPERATIONS = {
    "CAMPAIGN": "build.get_campaigns",
    "ADGROUP": "build.get_adgroups",
    "AD": "build.get_ads",
    "CTA": "build.get_cta_portfolio",
    "ADGROUP_STATUS": "build.get_regular_adgroups",
}


def read_arguments(query: BuildReadQuery) -> tuple[str, dict[str, object]]:
    """精确 ID 查询仍限定父级；CTA 无已知 ID 时不假造可完整扫描的工具。"""
    kind = query.intent.kind
    body = encode_intent(query.intent)
    arguments: dict[str, object] = {"advertiser_id": query.intent.advertiser_id}
    if kind == "CTA":
        if query.remote_id is None or query.page != 1:
            raise RemoteCallError(
                "cta_unknown_id", effect="NOT_SENT", evidence=CallEvidence()
            )
        arguments["creative_portfolio_id"] = query.remote_id
    else:
        identifiers = {
            "CAMPAIGN": "campaign_ids",
            "ADGROUP": "adgroup_ids",
            "AD": "smart_plus_ad_ids",
        }
        names = {"CAMPAIGN": "campaign_name", "ADGROUP": "adgroup_name"}
        filtering: dict[str, object] = {}
        if query.remote_id is not None:
            filtering[identifiers[kind]] = [query.remote_id]
        elif kind in names:
            filtering[names[kind]] = body[names[kind]]
        if isinstance(query.intent, AdGroupCreate):
            filtering["campaign_ids"] = [query.intent.campaign_id]
        elif isinstance(query.intent, AdCreate):
            filtering["adgroup_ids"] = [query.intent.adgroup_id]
        fields = list(body)
        fields.append(identifiers[kind][:-1])
        # 固定 Smart+ 广告组 GET 的 fields 不支持 operation_status；另走普通组状态查询。
        if kind == "ADGROUP":
            fields.remove("operation_status")
        arguments.update(
            filtering=filtering, page=query.page, page_size=100, fields=fields
        )
    return READ_OPERATIONS[kind], arguments


class _StatusQuery(FrozenModel):
    advertiser_id: Id
    adgroup_id: Id


def status_arguments(*, advertiser_id: str, adgroup_id: str) -> dict[str, object]:
    _StatusQuery(advertiser_id=advertiser_id, adgroup_id=adgroup_id)
    return {
        "advertiser_id": advertiser_id,
        "filtering": {"adgroup_ids": [adgroup_id]},
        "fields": ["advertiser_id", "adgroup_id", "operation_status"],
        "page": 1,
        "page_size": 100,
    }
