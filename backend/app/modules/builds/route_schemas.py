"""页面只展示实际冻结的归属，不返回授权材料或内部版本。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.integrations.tiktok.contracts.context import ChannelKind


class ExecutionRoutePublic(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    connection_id: UUID
    connection_name: str
    channel: ChannelKind
    bc_id: str
