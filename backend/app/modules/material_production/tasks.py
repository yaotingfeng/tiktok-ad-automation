from typing import Any
from uuid import UUID

from sqlmodel import Session

from app.core.db import engine
from app.core.errors import DomainError
from app.jobs.celery_app import celery_app

from .service import provision_once, recover_due


@celery_app.task(
    name="material_production.provision", time_limit=45, soft_time_limit=40
)
def provision_task(*, tenant_id: str, actor_id: str, payload: dict[str, Any]) -> None:
    try:
        if (
            set(payload) != {"binding_id", "sequence"}
            or UUID(payload["binding_id"]) != UUID(tenant_id)
            or type(payload["sequence"]) is not int
            or payload["sequence"] < 1
        ):
            raise ValueError
        tenant, actor = UUID(tenant_id), UUID(actor_id)
    except ValueError, TypeError, KeyError:
        raise DomainError(
            "material_onboarding_invalid", "素材开户任务参数无效"
        ) from None
    with Session(engine) as session, session.begin():
        provision_once(
            session, tenant_id=tenant, actor_id=actor, sequence=payload["sequence"]
        )


@celery_app.task(name="material_production.recover", time_limit=30, soft_time_limit=25)
def recover_task() -> int:
    with Session(engine) as session, session.begin():
        return recover_due(session)
