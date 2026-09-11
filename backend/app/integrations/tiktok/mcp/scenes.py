"""官方 MCP 场景读取；不解析自然语言成功。"""

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.accounts import RuntimeReadContext
from app.integrations.tiktok.contracts.scenes import ScenePage
from app.integrations.tiktok.read_normalization import scene_arguments, scene_page
from app.modules.builds.scene_schemas import SceneResource

from .transport import BoundMCPClient


class McpScenesGateway:
    def __init__(self, client: BoundMCPClient, *, context: RuntimeReadContext):
        if type(context) is not RuntimeReadContext:
            raise DomainError("read_context_invalid", "场景需要绑定 BC")
        self._client = client
        self._context = context

    def read_page(
        self,
        *,
        resource: SceneResource,
        advertiser_id: str,
        page: int,
        minis_id: str | None,
    ) -> ScenePage:
        operation, arguments = scene_arguments(
            resource=resource,
            advertiser_id=advertiser_id,
            bc_id=self._context.bc_id,
            page=page,
            minis_id=minis_id,
        )
        response = self._client.call(
            operation=operation,
            advertiser_id=arguments.get("advertiser_id"),
            arguments=arguments,
        )
        return scene_page(
            response.data,
            evidence=response.evidence,
            resource=resource,
            advertiser_id=advertiser_id,
            bc_id=self._context.bc_id,
            page=page,
            minis_id=minis_id,
        )
