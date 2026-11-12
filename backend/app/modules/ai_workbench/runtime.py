"""DSH 独立进程接入：服务身份领取、任务级短令牌、结构化完成及版本发布。"""

import hmac
import json
from datetime import UTC, datetime
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

import jwt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, text
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, select

from app.api.deps import SessionDep
from app.core import security
from app.core.config import settings
from app.core.context import TenantContext
from app.core.errors import DomainError
from app.modules.ai_workbench import ai_tasks, proposals, reviews
from app.modules.ai_workbench.knowledge_refs import sanitize_public
from app.modules.ai_workbench.methods import MethodBundle
from app.modules.ai_workbench.models import AIOptimizer, AIOptimizerVersion
from app.modules.ai_workbench.proposals import ProposalInput

SERVICE_AUDIENCE = "tkada-ai-review"
router = APIRouter(
    prefix="/integrations/ai-workbench/reviews", tags=["ai_workbench_runtime"]
)
bearer = HTTPBearer(auto_error=False)


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    problem: str = Field(min_length=1, max_length=2000)
    suggestion: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=30)
    validation: str = Field(default="", max_length=2000)
    capability_request: str | None = Field(default=None, max_length=100)


class ExperienceCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=4000)
    applicability: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[UUID] = Field(min_length=1, max_length=30)
    validation: str = Field(min_length=1, max_length=2000)


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=20000)
    proposals: list[ProposalInput] = Field(default_factory=list, max_length=20)
    recommendations: list[Recommendation] = Field(default_factory=list, max_length=20)
    experience_candidates: list[ExperienceCandidate] = Field(
        default_factory=list, max_length=10
    )
    method_usage: list[str] = Field(default_factory=list, max_length=6)
    failed: bool = False


def _credentials(value: HTTPAuthorizationCredentials | None) -> str:
    if value is None or value.scheme.lower() != "bearer":
        raise HTTPException(401, "Missing runtime authorization")
    return value.credentials


def service_identity(
    value: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> None:
    supplied = _credentials(value)
    if not settings.AI_DSH_SERVICE_TOKEN:
        raise HTTPException(503, "DSH runtime is not configured")
    if not hmac.compare_digest(supplied, settings.AI_DSH_SERVICE_TOKEN):
        raise HTTPException(403, "Invalid runtime authorization")


def _context(task: reviews.DSHReviewTask) -> TenantContext:
    return TenantContext(tenant_id=task.tenant_id, actor_id=task.created_by, role="")


def _task_token(task: reviews.DSHReviewTask) -> str:
    return jwt.encode(
        {
            "aud": SERVICE_AUDIENCE,
            "sub": str(task.id),
            "tenant": str(task.tenant_id),
            "optimizer": str(task.optimizer_id),
            "fence": task.fence,
            "exp": task.authorization_expires_at,
        },
        settings.SECRET_KEY,
        algorithm=security.ALGORITHM,
    )


def task_identity(
    task_id: UUID,
    session: SessionDep,
    value: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> reviews.DSHReviewTask:
    token = _credentials(value)
    try:
        claims = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[security.ALGORITHM],
            audience=SERVICE_AUDIENCE,
        )
    except jwt.InvalidTokenError:
        raise HTTPException(403, "Invalid task authorization") from None
    task = session.exec(
        select(reviews.DSHReviewTask)
        .where(reviews.DSHReviewTask.id == task_id)
        .with_for_update()
    ).one_or_none()
    if task is None or any(
        claims.get(key) != str(value)
        for key, value in {
            "sub": task.id,
            "tenant": task.tenant_id,
            "optimizer": task.optimizer_id,
            "fence": task.fence,
        }.items()
    ):
        raise HTTPException(403, "Task authorization scope mismatch")
    ai_tasks._check_fence(task, fence=claims["fence"])
    reviews.authorize(session, _context(task), write=True)
    optimizer, _ = reviews.load_optimizer(session, _context(task), task.optimizer_id)
    if not optimizer.active:
        raise DomainError("ai_optimizer_inactive", "AI 优化师已暂停")
    return task


@router.post("/claim", dependencies=[Depends(service_identity)])
def claim(session: SessionDep, methods: MethodBundle) -> dict:
    now = datetime.now(UTC)
    # 跨进程只允许同一优化师一个复盘会话；迟到结果由新 fence 拒绝。
    candidates = session.exec(
        select(reviews.DSHReviewTask)
        .where(
            or_(
                reviews.DSHReviewTask.status == "PENDING",
                (reviews.DSHReviewTask.status == "RUNNING")
                & (reviews.DSHReviewTask.authorization_expires_at <= now),
            )
        )
        .order_by(reviews.DSHReviewTask.created_at)
        .limit(100)
        .with_for_update(skip_locked=True)
    ).all()
    for task in candidates:
        acquired = SASession.execute(
            session,
            text("SELECT pg_try_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"ai-review:{task.tenant_id}:{task.optimizer_id}"},
        ).scalar()
        if not acquired:
            continue
        busy = session.exec(
            select(reviews.DSHReviewTask.id).where(
                reviews.DSHReviewTask.tenant_id == task.tenant_id,
                reviews.DSHReviewTask.optimizer_id == task.optimizer_id,
                reviews.DSHReviewTask.id != task.id,
                reviews.DSHReviewTask.status == "RUNNING",
                reviews.DSHReviewTask.authorization_expires_at > now,
            )
        ).first()
        if busy is not None:
            continue
        try:
            context = _context(task)
            optimizer, version = reviews.load_optimizer(
                session, context, task.optimizer_id
            )
            reviews.authorize(session, context, write=True)
            if not optimizer.active or version.number != task.optimizer_version:
                task.status = "CANCELLED"
                task.cancellation_requested = True
                session.add(task)
                continue
            if task.authorization_expires_at <= now:
                task = ai_tasks.restart_task(
                    session,
                    context=context,
                    optimizer_id=task.optimizer_id,
                    task_id=task.id,
                )
            ai_tasks.start_task(
                session,
                context=context,
                optimizer_id=task.optimizer_id,
                task_id=task.id,
                fence=task.fence,
            )
        except DomainError:
            task.status = "CANCELLED"
            task.cancellation_requested = True
            session.add(task)
            continue
        if "methods" not in task.evidence:
            # 首次领取时绑定插件实际发布的方法；重试/升级沿用已归档正文，
            # 不覆盖创建时冻结的业务数据，也不让新方法改变旧轮结论。
            from app.modules.ai_workbench.input_budget import MAX_INPUT_BYTES

            bound_evidence = {
                **task.evidence,
                "methods": methods.model_dump(mode="json"),
            }
            if (
                len(
                    json.dumps(
                        bound_evidence, ensure_ascii=False, separators=(",", ":")
                    ).encode()
                )
                > MAX_INPUT_BYTES
            ):
                raise DomainError(
                    "ai_review_input_too_large",
                    "绑定方法后输入超过容量，请创建较小范围的新任务",
                )
            task.evidence = bound_evidence
            session.add(task)
        payload = {
            "task": {
                "id": str(task.id),
                "tenant_id": str(task.tenant_id),
                "optimizer_id": str(task.optimizer_id),
                "fence": task.fence,
                "expires_at": task.authorization_expires_at.isoformat(),
                "evidence": task.evidence,
            },
            "task_token": _task_token(task),
        }
        session.commit()
        return payload
    session.commit()
    return {"task": None}


@router.post("/{task_id}/complete")
def complete(
    task_id: UUID,
    body: ReviewResult,
    session: SessionDep,
    task: reviews.DSHReviewTask = Depends(task_identity),
) -> dict:
    if task.id != task_id:
        raise HTTPException(403, "Task scope mismatch")
    context = _context(task)
    result = sanitize_public(body.model_dump(mode="json"))
    if task.status in {"COMPLETE", "FAILED"}:
        if task.result != result:
            raise DomainError("idempotency_conflict", "同一复盘任务不能提交不同结果")
        return {"id": str(task.id), "status": task.status}
    if task.status != "RUNNING":
        raise DomainError("ai_task_fence_conflict", "复盘任务未领取")
    if body.method_usage:
        bound = task.evidence.get("methods", {})
        method_keys = {
            r["key"]
            for r in [bound.get("main", {}), *bound.get("references", [])]
            if r.get("key")
        }
        if not set(body.method_usage).issubset(method_keys):
            raise DomainError("ai_method_not_bound", "方法不属于此任务绑定清单")
    cited = {
        str(e)
        for item in [*body.recommendations, *body.experience_candidates]
        for e in item.evidence_ids
    }
    if not cited.issubset(reviews.frozen_evidence_ids(task.evidence)):
        raise DomainError("ai_evidence_not_found", "建议或经验引用了任务范围外的证据")
    if body.failed and (body.proposals or body.experience_candidates):
        raise DomainError("ai_proposal_invalid", "失败复盘不能发布提案")
    for index, change in enumerate(body.proposals):
        proposals.create_proposal(
            session,
            context=context,
            optimizer_id=task.optimizer_id,
            review_task=task,
            change=change,
            request_id=uuid5(task.id, f"proposal:{index}"),
        )
    # 所有提案以同一冻结基线入库，再统一按当前模式及授权决定发布。
    if not body.failed:
        proposals.publish_auto_proposals(
            session,
            context=context,
            optimizer_id=task.optimizer_id,
            review_task_id=task.id,
        )
    ai_tasks.finish_task(
        session,
        context=context,
        optimizer_id=task.optimizer_id,
        task_id=task.id,
        fence=task.fence,
        result=result,
    )
    if body.failed:
        task.status = "FAILED"
        session.add(task)
    session.commit()
    return {"id": str(task.id), "status": task.status}


def schedule_reviews(session: Session, *, now: datetime, limit: int = 100) -> int:
    if not settings.AI_DSH_SERVICE_TOKEN:
        return 0
    count = 0
    rows = session.exec(
        select(AIOptimizer).where(AIOptimizer.active.is_(True)).order_by(AIOptimizer.id)
    ).all()
    for optimizer in rows:
        version = session.exec(
            select(AIOptimizerVersion).where(
                AIOptimizerVersion.tenant_id == optimizer.tenant_id,
                AIOptimizerVersion.optimizer_id == optimizer.id,
                AIOptimizerVersion.number == optimizer.latest_version,
            )
        ).one()
        local = now.astimezone(ZoneInfo(version.timezone))
        if local.strftime("%H:%M") < version.review_time:
            continue
        request_id = uuid5(optimizer.id, f"review:{local.date()}")
        if session.exec(
            select(reviews.DSHReviewTask.id).where(
                reviews.DSHReviewTask.tenant_id == optimizer.tenant_id,
                reviews.DSHReviewTask.request_id == request_id,
            )
        ).first():
            continue
        try:
            with session.begin_nested():
                reviews.create_review_task(
                    session,
                    context=TenantContext(
                        tenant_id=optimizer.tenant_id,
                        actor_id=optimizer.created_by,
                        role="",
                    ),
                    optimizer_id=optimizer.id,
                    request_id=request_id,
                )
                count += 1
        except DomainError:
            continue
        if count >= limit:
            break
    return count


@router.post("/{task_id}/heartbeat")
def heartbeat(
    task_id: UUID, task: reviews.DSHReviewTask = Depends(task_identity)
) -> dict:
    if task.id != task_id or task.status != "RUNNING":
        raise DomainError("ai_task_fence_conflict", "复盘任务已停止")
    return {"active": True, "expires_at": task.authorization_expires_at}
