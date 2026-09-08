from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.modules.accounts.capability_models import CapabilityJob


class CapabilityRefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: UUID
    connection_id: UUID


class CapabilityJobPublic(BaseModel):
    model_config = ConfigDict(extra="forbid")
    job_id: UUID
    bc_id: str
    connection_id: UUID
    status: Literal["PENDING", "COMPLETE", "BLOCKED", "STALE", "FAILED"]
    phase: Literal["READ", "PUBLISH", "DONE"]
    remote_read_count: int
    remote_total_count: int | None
    published_account_count: int
    permissions_known: bool
    error_code: str | None
    created_at: datetime
    completed_at: datetime | None


def public_job(job: CapabilityJob) -> CapabilityJobPublic:
    return CapabilityJobPublic.model_validate(
        {
            "job_id": job.id,
            "bc_id": job.bc_id,
            "connection_id": job.connection_id,
            "status": job.status,
            "phase": job.phase,
            "remote_read_count": job.seen_count,
            "remote_total_count": job.total_count,
            "published_account_count": job.published_count,
            "permissions_known": job.scope_known,
            "error_code": job.error_code,
            "created_at": job.created_at,
            "completed_at": job.completed_at,
        }
    )


@dataclass(frozen=True)
class CapabilityEvidence:
    job_id: UUID
    evidence_ids: tuple[UUID, ...]
    page_number: int | None
    observed_at: datetime
    expires_at: datetime
    scope_verified: bool
    can_build: bool
    can_upload: bool
    source_revision: str
    basis_digest: str
