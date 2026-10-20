from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class ProductionStatus(BaseModel):
    status: Literal["not_configured", "pending", "ready", "failed"]
    username: str | None = None
    company_id: str | None = None
    error_code: str | None = None
    message: str | None = None
    attempts: int = 0


class ProductionLoginLink(BaseModel):
    login_url: str = Field(repr=False)
    expires_at: datetime
