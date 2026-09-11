from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

ChannelKind = Literal["OFFICIAL_API", "OFFICIAL_MCP"]


class FrozenTikTokRoute(BaseModel):
    """冻结执行归属与授权语义；凭据轮换不改变已选定连接。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: UUID
    bc_id: str
    connection_id: UUID
    channel: ChannelKind
    authorization_revision: int
    adapter_contract_revision: str
