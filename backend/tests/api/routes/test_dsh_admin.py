from datetime import UTC, datetime, timedelta

import jwt
from sqlmodel import select

from app.api.routes.dsh_admin import AUDIENCE, COOKIE
from app.core import security
from app.core.config import settings
from app.models import User


def test_session_requires_platform_admin(client, normal_user_token_headers):
    assert client.post("/api/admin/dsh/session").status_code == 401
    assert client.post(
        "/api/admin/dsh/session", headers=normal_user_token_headers
    ).status_code == 403


def test_admin_cookie_is_scoped_and_role_revocation_is_immediate(
    client, session, superuser_token_headers
):
    result = client.post("/api/admin/dsh/session", headers=superuser_token_headers)
    assert result.status_code == 200
    assert result.json() == {"entry_url": "/dsh/"}
    cookie = result.headers["set-cookie"]
    for attribute in ["HttpOnly", "Secure", "SameSite=strict", "Path=/dsh/", "Max-Age=900"]:
        assert attribute in cookie
    credential = result.cookies[COOKIE]
    headers = {"cookie": f"{COOKIE}={credential}"}
    assert client.get("/api/admin/dsh/authorize", headers=headers).status_code == 204
    payload = jwt.decode(
        credential, settings.SECRET_KEY, algorithms=[security.ALGORITHM], audience=AUDIENCE
    )
    user = session.get(User, payload["sub"])
    user.is_superuser = False
    session.add(user)
    session.flush()
    assert client.get("/api/admin/dsh/authorize", headers=headers).status_code == 403
    user.is_superuser = True
    user.is_active = False
    session.flush()
    assert client.get("/api/admin/dsh/authorize", headers=headers).status_code == 403
    # DSH Cookie 不能作为普通 API 的 Bearer 登录令牌。
    assert client.get(
        "/api/users/me", headers={"Authorization": f"Bearer {credential}"}
    ).status_code == 401


def test_invalid_expired_and_wrong_audience_are_denied(client, session):
    user = session.exec(select(User)).first()
    for token in [
        "invalid",
        security.create_access_token(user.id, timedelta(minutes=1)),
        jwt.encode(
            {"sub": str(user.id), "aud": AUDIENCE, "exp": datetime.now(UTC) - timedelta(seconds=1)},
            settings.SECRET_KEY, algorithm=security.ALGORITHM,
        ),
    ]:
        assert client.get(
            "/api/admin/dsh/authorize", headers={"cookie": f"{COOKIE}={token}"}
        ).status_code == 401
    assert client.get("/api/admin/dsh/authorize").status_code == 401
