"""一次派发推进一个有界只读阶段；每个 BC 的 MCP 读取结果仅写 staging。"""

import copy
import re
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from billiard.process import current_process  # type: ignore[import-untyped]
from redis import Redis
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session as SASession
from sqlmodel import Session, select

from app.core.config import settings
from app.core.context import TenantContext
from app.core.db import engine
from app.core.errors import DomainError
from app.integrations.tiktok.bounded_resources import bounded_session
from app.integrations.tiktok.contracts.common import CallEvidence
from app.integrations.tiktok.contracts.discovery import (
    AUTHORIZED_LIST_SOURCE,
    DISCOVERY_PAGE_SIZE,
    DiscoveryAccountsGateway,
)
from app.integrations.tiktok.mcp_auth.management import open_management_accounts
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.jobs.celery_app import celery_app
from app.jobs.models import PendingDispatch
from app.jobs.outbox import enqueue_after_commit
from app.jobs.tasks import register_dispatch_task
from app.modules.accounts.discovery_models import DiscoveryStagedPage
from app.modules.accounts.mcp_discovery import (
    incomplete,
    locked_mcp_run,
    mark_mcp_run_error,
    publish_mcp_directory,
    verified_observation,
)
from app.modules.accounts.models import DiscoveryRun

register_dispatch_task("accounts.mcp_discover", "resources")
HARD_LIMIT_SECONDS = 45
RECOVERY_SECONDS = 60


def _queue(
    session: Session,
    *,
    run: DiscoveryRun,
    suffix: str = "step",
    due: datetime | None = None,
) -> None:
    identity = enqueue_after_commit(
        session,
        context=TenantContext(
            tenant_id=run.tenant_id, actor_id=run.actor_id, role="tenant_admin"
        ),
        task_name="accounts.mcp_discover",
        task_key=f"mcp-discover:{run.id}:{run.revision}:{suffix}",
        payload={"run_id": str(run.id), "revision": run.revision},
    )
    if due is not None:
        dispatch = session.get(PendingDispatch, identity)
        assert dispatch is not None
        dispatch.available_at = due
        session.add(dispatch)


def _evidence(evidence: CallEvidence, *, operation: str) -> dict[str, Any]:
    values: dict[str, Any] = {"operation": operation}
    for key, value in asdict(evidence).items():
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            values[key] = value
    return values


def _page_result(page: Any, *, stage: str, operation: str) -> dict[str, Any]:
    if page.page_size != DISCOVERY_PAGE_SIZE:
        raise incomplete()
    return {
        "stage": stage,
        "page": page.page,
        "total_pages": max(1, page.total_pages),
        "total_number": page.total_number,
        "last_page": page.last,
        "pagination_kind": "REMOTE",
        "rows": [asdict(item) for item in page.items],
        "call_evidence": _evidence(page.evidence, operation=operation),
    }


def read_discovery_stage(
    gateway: Any, *, work: dict[str, Any], requested_ids: tuple[str, ...]
) -> list[dict[str, Any]]:
    stage = work["stage"]
    if stage == "DETAILS" and not requested_ids:
        return [
            {
                "stage": "DETAILS",
                "page": work["page"],
                "total_pages": work["asset_total_pages"],
                "total_number": None,
                "last_page": work["asset_last_page"],
                "pagination_kind": "EXPLICIT_IDS",
                "rows": [],
                "call_evidence": {
                    "operation": "accounts.get_advertisers",
                    "empty_authorized_intersection": True,
                },
            }
        ]
    if not isinstance(gateway, DiscoveryAccountsGateway):
        raise DomainError(
            "unsupported_account_schema", "当前账户适配器尚未支持分阶段发现"
        )
    if stage in {"MCP_VERIFY", "SUBJECT"}:
        observed = gateway.observe_authorization()
        facts = asdict(observed.facts)
        facts["observed_at"] = observed.facts.observed_at.isoformat()
        facts["scopes"] = list(observed.facts.scopes)
        return [
            {
                "stage": "SUBJECT",
                "page": 1,
                "total_pages": 1,
                "total_number": 1,
                "last_page": True,
                "pagination_kind": "FULL_RESPONSE",
                "rows": [facts],
                "call_evidence": _evidence(
                    observed.evidence, operation="accounts.authorization_facts"
                ),
            }
        ]
    if stage == "AUTHORIZED":
        observed_ids = gateway.authorized_advertisers()
        ids = observed_ids.advertiser_ids
        count = max(1, (len(ids) + DISCOVERY_PAGE_SIZE - 1) // DISCOVERY_PAGE_SIZE)
        evidence = {
            **_evidence(
                observed_ids.evidence,
                operation="accounts.list_authorized_advertisers",
            ),
            "completeness_source": AUTHORIZED_LIST_SOURCE,
        }
        return [
            {
                "stage": "AUTHORIZED",
                "page": page + 1,
                "total_pages": count,
                "total_number": len(ids),
                "last_page": page + 1 == count,
                "pagination_kind": "FULL_RESPONSE",
                "rows": [
                    {"advertiser_id": identity}
                    for identity in ids[
                        page * DISCOVERY_PAGE_SIZE : (page + 1) * DISCOVERY_PAGE_SIZE
                    ]
                ],
                "call_evidence": evidence,
            }
            for page in range(count)
        ]
    if stage == "BCS":
        return [
            _page_result(
                gateway.discovery_business_centers(
                    page=work["page"], page_size=DISCOVERY_PAGE_SIZE
                ),
                stage="BCS",
                operation="accounts.list_bcs",
            )
        ]
    if stage == "ASSETS":
        return [
            _page_result(
                gateway.assets(
                    bc_id=work["bc_id"],
                    page=work["page"],
                    page_size=DISCOVERY_PAGE_SIZE,
                ),
                stage="ASSETS",
                operation="accounts.list_bc_assets",
            )
        ]
    if stage == "DETAILS":
        observed_details = gateway.advertiser_details(advertiser_ids=requested_ids)
        return [
            {
                "stage": "DETAILS",
                "page": work["page"],
                "total_pages": work["asset_total_pages"],
                "total_number": None,
                "last_page": work["asset_last_page"],
                "pagination_kind": "EXPLICIT_IDS",
                "rows": [asdict(item) for item in observed_details.items],
                "call_evidence": _evidence(
                    observed_details.evidence, operation="accounts.get_advertisers"
                ),
            }
        ]
    if stage == "ROLES":
        return [
            _page_result(
                gateway.roles(
                    bc_id=work["bc_id"],
                    page=work["page"],
                    page_size=DISCOVERY_PAGE_SIZE,
                ),
                stage="ROLES",
                operation="accounts.list_bc_assets",
            )
        ]
    raise incomplete()


def _read_stage(
    *,
    database_engine: Any,
    redis_client: Redis,
    context: TenantContext,
    connection_id: UUID,
    authorization_revision: int,
    run_id: UUID,
    deadline: datetime,
    work: dict[str, Any],
    requested_ids: tuple[str, ...],
) -> list[dict[str, Any]]:
    if work["stage"] == "DETAILS" and not requested_ids:
        return read_discovery_stage(None, work=work, requested_ids=requested_ids)
    with open_management_accounts(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        connection_id=connection_id,
        authorization_revision=authorization_revision,
        run_id=run_id,
        task_deadline=deadline,
    ) as gateway:
        return read_discovery_stage(gateway, work=work, requested_ids=requested_ids)


def _detail_ids(session: Session, *, run: DiscoveryRun) -> tuple[str, ...]:
    page = session.get(
        DiscoveryStagedPage, (run.id, run.work["bc_id"], "ASSETS", run.work["page"])
    )
    if page is None:
        raise incomplete()
    ids = [row["advertiser_id"] for row in page.rows]
    if not ids:
        return ()
    result = (
        SASession.execute(
            session,
            text("""
        SELECT item->>'advertiser_id' FROM discovery_staged_page p
        CROSS JOIN LATERAL jsonb_array_elements(p.rows) item
        WHERE p.run_id=:run_id AND p.stage='AUTHORIZED' AND item->>'advertiser_id' IN :ids
    """).bindparams(bindparam("ids", expanding=True)),
            {"run_id": run.id, "ids": ids},
        )
        .scalars()
        .all()
    )
    return tuple(sorted(result))


def stage_results(
    session: Session,
    *,
    run: DiscoveryRun,
    rows: list[dict[str, Any]],
    schema_digest: str,
    max_pages: int = 2000,
) -> None:
    if not rows:
        raise incomplete()
    staged: list[DiscoveryStagedPage] = []
    first = rows[0]
    stage = first["stage"]
    bc_id = run.work["bc_id"] if stage in {"ASSETS", "DETAILS", "ROLES"} else ""
    if (
        session.exec(
            select(DiscoveryStagedPage)
            .where(
                DiscoveryStagedPage.run_id == run.id,
                DiscoveryStagedPage.bc_id == bc_id,
                DiscoveryStagedPage.stage == stage,
                DiscoveryStagedPage.page >= first["page"],
            )
            .limit(1)
        ).first()
        is not None
    ):
        raise DomainError("discovery_stale", "此阶段已完成暂存")
    prior = (
        session.get(DiscoveryStagedPage, (run.id, bc_id, stage, first["page"] - 1))
        if first["page"] > 1
        else None
    )
    for offset, result in enumerate(rows):
        page_no = result["page"]
        if (
            result["stage"] != stage
            or type(page_no) is not int
            or page_no != first["page"] + offset
            or not 1 <= page_no <= max_pages
        ):
            raise incomplete()
        if page_no > 1 and (
            prior is None
            or prior.last_page
            or (prior.total_pages, prior.total_number)
            != (result["total_pages"], result["total_number"])
        ):
            raise incomplete()
        item = DiscoveryStagedPage(
            tenant_id=run.tenant_id,
            connection_id=run.connection_id,
            run_id=run.id,
            bc_id=bc_id,
            schema_digest=schema_digest,
            **result,
        )
        staged.append(item)
        prior = item
    # 完整无分页响应的内部50条分片批量落库，避免100k授权ID产生4000次短事务查询。
    session.add_all(staged)
    session.flush()


def _save_results(
    session: Session, *, run: DiscoveryRun, rows: list[dict[str, Any]]
) -> None:
    observation = verified_observation(session, run=run)
    stage_results(
        session,
        run=run,
        rows=rows,
        schema_digest=observation.schema_digest,
        max_pages=2000 if rows and rows[0]["stage"] == "AUTHORIZED" else 1000,
    )
    advance_discovery_stage(run, rows)


def advance_discovery_stage(run: DiscoveryRun, rows: list[dict[str, Any]]) -> None:
    work = copy.deepcopy(run.work)
    result = rows[-1]
    stage = result["stage"]
    if stage == "SUBJECT":
        work.update(stage="AUTHORIZED", page=1)
    elif stage == "AUTHORIZED":
        work.update(stage="BCS", page=1)
    elif stage == "BCS":
        work.update(
            stage="ASSETS" if result["last_page"] else "BCS",
            page=1 if result["last_page"] else result["page"] + 1,
        )
    elif stage == "ASSETS":
        work.update(
            stage="DETAILS",
            asset_last_page=result["last_page"],
            asset_total_pages=result["total_pages"],
        )
    elif stage == "DETAILS":
        work.update(
            stage="ROLES" if result["last_page"] else "ASSETS",
            page=1 if result["last_page"] else result["page"] + 1,
        )
        work.pop("asset_last_page", None)
        work.pop("asset_total_pages", None)
    elif stage == "ROLES":
        work.update(
            stage="FINALIZE" if result["last_page"] else "ROLES",
            page=1 if result["last_page"] else result["page"] + 1,
        )
    run.work = work


def process_mcp_discovery(
    *,
    database_engine: Any,
    redis_client: Redis,
    tenant_id: UUID,
    actor_id: UUID,
    payload: dict[str, Any],
) -> None:
    try:
        if set(payload) - {"run_id", "revision"}:
            raise ValueError("unexpected payload")
        run_id = UUID(payload["run_id"])
        expected_revision = payload.get("revision", 0)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("invalid revision")
    except KeyError, ValueError, TypeError:
        raise DomainError("dispatch_payload_invalid", "目录任务参数无效") from None
    context = TenantContext(tenant_id=tenant_id, actor_id=actor_id, role="tenant_admin")
    deadline = datetime.now(UTC) + timedelta(seconds=30)
    claim_id = uuid4()
    with bounded_session(database_engine, task_deadline=deadline) as session:
        try:
            run, _ = locked_mcp_run(session, context=context, run_id=run_id)
        except DomainError as error:
            if error.code in {"tenant_forbidden", "action_forbidden"}:
                # 原管理员在首次派发前撤权也必须留下可重试终态；只写原租户、
                # 原代数且尚未被其他 claim 占用的任务，不能借拒绝结果跨越解绑围栏。
                try:
                    rejected, _ = locked_mcp_run(
                        session, context=context, run_id=run_id, check_admin=False
                    )
                except DomainError:
                    raise error from None
                now = datetime.now(UTC)
                if (
                    rejected.status in {"RUNNING", "ADMISSION_WAIT"}
                    and rejected.revision == expected_revision
                    and (
                        (rejected.claim_id is None and rejected.claimed_until is None)
                        or (
                            rejected.claimed_until is not None
                            and rejected.claimed_until <= now
                        )
                    )
                ):
                    mark_mcp_run_error(session, run=rejected, code=error.code)
                    session.commit()
            raise
        if run.status == "COMPLETE" or run.revision != expected_revision:
            return
        now = datetime.now(UTC)
        if (run.claimed_until and run.claimed_until > now) or (
            run.next_attempt_at and run.next_attempt_at > now
        ):
            return
        try:
            verified_observation(session, run=run)
        except DomainError as error:
            mark_mcp_run_error(session, run=run, code=error.code)
            session.commit()
            return
        work = copy.deepcopy(run.work)
        connection_id = run.connection_id
        authorization_revision = run.authorization_revision
        requested_ids = (
            _detail_ids(session, run=run) if work["stage"] == "DETAILS" else ()
        )
        run.claim_id = claim_id
        run.claimed_until = now + timedelta(seconds=RECOVERY_SECONDS)
        if work["stage"] != "FINALIZE":
            run.sent_count += 1
        session.add(run)
        _queue(session, run=run, suffix=f"recover:{claim_id}", due=run.claimed_until)
        session.commit()
    try:
        if work["stage"] == "FINALIZE":
            with bounded_session(database_engine, task_deadline=deadline) as session:
                publish_mcp_directory(
                    session,
                    context=context,
                    run_id=run_id,
                    claim_id=claim_id,
                    revision=expected_revision,
                )
                session.commit()
            return
        results = _read_stage(
            database_engine=database_engine,
            redis_client=redis_client,
            context=context,
            connection_id=connection_id,
            authorization_revision=authorization_revision,
            run_id=run_id,
            deadline=deadline,
            work=work,
            requested_ids=requested_ids,
        )
        with bounded_session(database_engine, task_deadline=deadline) as session:
            run, _ = locked_mcp_run(session, context=context, run_id=run_id)
            if run.claim_id != claim_id or run.revision != expected_revision:
                return
            _save_results(session, run=run, rows=results)
            run.claim_id = None
            run.claimed_until = None
            run.next_attempt_at = None
            run.revision += 1
            run.status = "RUNNING"
            session.add(run)
            _queue(session, run=run)
            session.commit()
    except Exception as error:
        code = error.code if isinstance(error, DomainError) else "discovery_failed"
        with bounded_session(
            database_engine, task_deadline=datetime.now(UTC) + timedelta(seconds=5)
        ) as session:
            # 撤权后仅允许原 claim 留失败审计；解绑或重授权后的迟到结果不回写。
            try:
                failed_run, _ = locked_mcp_run(
                    session, context=context, run_id=run_id, check_admin=False
                )
            except DomainError:
                return
            if (
                failed_run is None
                or failed_run.claim_id != claim_id
                or failed_run.revision != expected_revision
                or failed_run.status not in {"RUNNING", "ADMISSION_WAIT"}
            ):
                return
            failed_run.claim_id = None
            failed_run.claimed_until = None
            failed_run.error_code = code
            if isinstance(error, AccountAdmissionDeferred) or code in {
                "admission_unavailable",
                "tiktok_local_resources_unavailable",
                # 兄弟 BC 轮换共享令牌时只重开读取会话，不让当前任务失效。
                "gateway_credentials_changed",
                "mcp_refresh_pending",
            }:
                failed_run.status = "ADMISSION_WAIT"
                failed_run.next_attempt_at = datetime.now(UTC) + timedelta(
                    milliseconds=max(
                        1,
                        error.retry_after_ms
                        if isinstance(error, AccountAdmissionDeferred)
                        else 5000,
                    )
                )
                failed_run.revision += 1
                _queue(session, run=failed_run, due=failed_run.next_attempt_at)
            else:
                mark_mcp_run_error(session, run=failed_run, code=code)
            session.add(failed_run)
            session.commit()


@celery_app.task(  # type: ignore[untyped-decorator]
    name="accounts.mcp_discover",
    bind=True,
    time_limit=HARD_LIMIT_SECONDS,
    soft_time_limit=40,
)
def discover_mcp(
    self: Any, *, tenant_id: str, actor_id: str, payload: dict[str, Any]
) -> None:
    limits = self.request.timelimit or (None, None)
    hard_limit = limits[0] or self.time_limit
    if (
        not current_process().daemon
        or not current_process().name.startswith("ForkPoolWorker-")
        or self.request.called_directly
        or self.request.is_eager
        or isinstance(hard_limit, bool)
        or not isinstance(hard_limit, (int, float))
        or not 0 < hard_limit <= HARD_LIMIT_SECONDS
    ):
        raise DomainError(
            "discovery_worker_unbounded", "账户发现需要启用 prefork 硬超时工作进程"
        )
    with Redis.from_url(settings.REDIS_URL) as redis_client:
        process_mcp_discovery(
            database_engine=engine,
            redis_client=redis_client,
            tenant_id=UUID(tenant_id),
            actor_id=UUID(actor_id),
            payload=payload,
        )
