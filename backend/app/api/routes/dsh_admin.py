"""DSH 官方管理面专用短会话，不授予租户管理员或 DSH 机器身份。"""

from datetime import UTC, datetime, timedelta
from typing import Annotated
from uuid import UUID

import jwt
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse
from jwt.exceptions import InvalidTokenError

from app.api.deps import SessionDep, get_current_active_superuser
from app.core import security
from app.core.config import settings
from app.models import User

router = APIRouter(prefix="/admin/dsh", tags=["admin-dsh"])
COOKIE = "tkada_dsh_admin"
AUDIENCE = "tkada-dsh-web"


@router.get("/entry", response_class=HTMLResponse, include_in_schema=False)
def entry() -> HTMLResponse:
    # 仅做同源令牌交换/跳转，不新增工作台页面，也不将登录令牌放入 URL。
    return HTMLResponse(
        """<!doctype html><meta charset="utf-8"><title>DSH</title>
<script>
(async () => {
  const token = localStorage.getItem('access_token');
  if (!token) { location.replace('/login'); return; }
  const response = await fetch('/api/admin/dsh/session', {
    method: 'POST', headers: {Authorization: 'Bearer ' + token}
  });
  if (response.ok) location.replace('/dsh/');
  else document.body.textContent = 'DSH 仅允许平台管理员访问，请重新登录或联系管理员。';
})().catch(() => { document.body.textContent = 'DSH 入口暂时不可用。'; });
</script><body></body>""",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.post("/session")
def create_session(
    response: Response,
    user: Annotated[User, Depends(get_current_active_superuser)],
) -> dict[str, str]:
    token = jwt.encode(
        {
            "sub": str(user.id),
            "aud": AUDIENCE,
            "exp": datetime.now(UTC) + timedelta(minutes=15),
        },
        settings.SECRET_KEY,
        algorithm=security.ALGORITHM,
    )
    # 仅 DSH 路径接收，网关内部只将此 cookie 传给权限端点。
    response.set_cookie(
        COOKIE, token, max_age=900, path="/dsh/", secure=True,
        httponly=True, samesite="strict",
    )
    response.headers["Cache-Control"] = "no-store"
    return {"entry_url": "/dsh/"}


@router.get("/authorize", status_code=204)
def authorize(
    session: SessionDep,
    response: Response,
    credential: Annotated[str | None, Cookie(alias=COOKIE)] = None,
) -> None:
    try:
        payload = jwt.decode(
            credential or "", settings.SECRET_KEY,
            algorithms=[security.ALGORITHM], audience=AUDIENCE,
            options={"require": ["exp", "sub", "aud"]},
        )
        user_id = UUID(payload["sub"])
    except (InvalidTokenError, ValueError, TypeError, KeyError):
        raise HTTPException(401, "DSH administrator session required") from None
    # 每次回查数据库，角色撤销和停用无需等待签名令牌到期。
    user = session.get(User, user_id)
    if not user or not user.is_active or not user.is_superuser:
        raise HTTPException(403, "Platform administrator required")
    response.headers["Cache-Control"] = "no-store"
