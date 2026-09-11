"""真实数据库中的第二条授权连接；不替换路由或 HTTP 边界。"""


def second_connection(session, env):
    from datetime import UTC, datetime

    from sqlmodel import select

    from app.modules.accounts.connection_models import (
        BCConnectionBinding,
        ConnectionAuthorization,
    )
    from app.modules.accounts.models import BCAccountAccess, TikTokConnection

    original = session.get(TikTokConnection, env["connection_id"])
    value = TikTokConnection(
        tenant_id=original.tenant_id,
        kind=original.kind,
        status="ACTIVE",
        credential_ciphertext=original.credential_ciphertext,
        authorization_revision=original.authorization_revision,
        adapter_contract_revision=original.adapter_contract_revision,
    )
    session.add(value)
    session.flush()
    session.add(
        BCConnectionBinding(
            tenant_id=value.tenant_id,
            bc_id=env["bc_id"],
            connection_id=value.id,
            kind=value.kind,
        )
    )
    session.add(
        ConnectionAuthorization(
            tenant_id=value.tenant_id,
            connection_id=value.id,
            authorization_revision=value.authorization_revision,
            scopes=["synthetic-material-scope"],
            source="SYNTHETIC_VERIFIED_EVIDENCE",
            issuer="https://business-api.tiktok.com",
            resource="https://business-api.tiktok.com/open_api/v1.3",
            verified_at=datetime.now(UTC),
            permission_summary={
                "read_authorized": True,
                "upload_authorized": True,
                "build_authorized": True,
            },
        )
    )
    for old in session.exec(
        select(BCAccountAccess).where(BCAccountAccess.connection_id == original.id)
    ).all():
        session.add(BCAccountAccess(**{**old.model_dump(), "connection_id": value.id}))
    session.flush()
    return value.id
