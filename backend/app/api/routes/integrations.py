from fastapi import APIRouter

from app.core.config import settings
from app.core.errors import DomainError

router = APIRouter(prefix="/integrations", tags=["integrations"])


@router.get("/tiktok/callback")
def tiktok_callback() -> None:
    """Stable callback address; tenant-bound state and SDK exchange arrive in P02."""
    settings.require_tiktok_app()
    raise DomainError("tiktok_oauth_unavailable", "授权接入尚未开放")
