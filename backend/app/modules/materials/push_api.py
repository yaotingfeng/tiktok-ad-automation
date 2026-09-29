"""独立验签入口，不接受浏览器登录令牌作为系统推送凭证。"""

from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.api.deps import SessionDep
from app.core.errors import DomainError

from .push_auth import authenticate
from .push_schemas import PushBatchInput, PushBatchPublic
from .push_service import read_batch, register_batch

router = APIRouter(prefix="/integrations/materials/batches", tags=["material-push"])
MAX_BODY_BYTES = 2 * 1024 * 1024
INPUT_SCHEMA = PushBatchInput.model_json_schema()
INPUT_SCHEMA["properties"]["materials"]["items"] = INPUT_SCHEMA.pop("$defs")[
    "PushMaterialInput"
]
SIGNATURE_HEADERS = [
    {
        "name": name,
        "in": "header",
        "required": True,
        "schema": {"type": "string"},
        "description": description,
    }
    for name, description in (
        ("X-Key-Id", "管理员分配的系统接入标识"),
        ("X-Timestamp", "Unix 秒，允许时钟偏差 300 秒"),
        ("X-Request-Id", "小写标准 UUID；POST 重试使用原请求标识"),
        ("X-Signature", "sha256=HMAC-SHA256 小写十六进制；详见接入文档"),
    )
]


async def read_signed_body(request: Request) -> bytes:
    if request.url.query:
        raise DomainError("push_invalid", "推送接口不接受查询参数")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_BYTES:
            raise DomainError("push_too_large", "请求体超过 2 MiB")
        body.extend(chunk)
    return bytes(body)


@router.post(
    "",
    response_model=PushBatchPublic,
    status_code=202,
    openapi_extra={
        "parameters": SIGNATURE_HEADERS,
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": INPUT_SCHEMA}},
        },
    },
)
async def post_material_batch(request: Request, session: SessionDep) -> PushBatchPublic:
    raw = await read_signed_body(request)
    identity = authenticate(
        method="POST", path=request.url.path, headers=request.headers, body=raw
    )
    if (
        request.headers.get("content-type", "").split(";", 1)[0].lower()
        != "application/json"
    ):
        raise DomainError("push_invalid", "请使用 application/json")
    try:
        body = PushBatchInput.model_validate_json(raw)
    except ValidationError:
        # 不回显含签名 URL 的 Pydantic input/context。
        raise DomainError(
            "push_invalid",
            "批次字段无效；请检查租户名称、素材数组、唯一 ID、文件名和 URL",
        ) from None

    def save() -> PushBatchPublic:
        value = register_batch(session, identity=identity, body=body, raw_body=raw)
        session.commit()
        return value

    return await run_in_threadpool(save)


@router.get(
    "/{batch_id}",
    response_model=PushBatchPublic,
    openapi_extra={"parameters": SIGNATURE_HEADERS},
)
async def get_material_batch(
    batch_id: UUID, request: Request, session: SessionDep
) -> PushBatchPublic:
    raw = await read_signed_body(request)
    if raw:
        raise DomainError("push_invalid", "查询请求体必须为空")
    identity = authenticate(
        method="GET", path=request.url.path, headers=request.headers, body=raw
    )
    return await run_in_threadpool(
        read_batch, session, identity=identity, batch_id=batch_id
    )
