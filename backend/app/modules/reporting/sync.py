"""报表采集步骤：每次只执行一个有界物理请求并持久化进度。"""

from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from sqlmodel import Session, select

from app.core.errors import DomainError
from app.integrations.tiktok.adapters.sdk_reporting import plan_report_shards
from app.integrations.tiktok.contracts.reporting import ReportTask
from app.modules.reporting.contracts import decode_query
from app.modules.reporting.facts import publish_report, stage_report_page
from app.modules.reporting.sync_models import ReportSyncRun

_WAIT_SECONDS = 30


def _run(session: Session, run_id: UUID) -> ReportSyncRun:
    row = session.exec(
        select(ReportSyncRun)
        .where(ReportSyncRun.id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        raise DomainError("report_run_not_found", "报表同步运行不存在")
    return row


def _wait(run: ReportSyncRun, *, task_status: str | None = None) -> str:
    run.status = "WAITING_REMOTE"
    run.coverage = "PENDING"
    run.next_attempt_at = datetime.now(UTC) + timedelta(seconds=_WAIT_SECONDS)
    if task_status is not None:
        run.task_status = task_status
    return "WAIT"


def _failed(run: ReportSyncRun, error: DomainError) -> str:
    run.status = "FAILED"
    run.coverage = "UNKNOWN"
    run.missing_reason = "UNKNOWN_OR_INCOMPLETE"
    run.error_code = error.code
    run.observed_at = datetime.now(UTC)
    return "FAILED"


def collect_report_step(
    session: Session,
    *,
    run_id: UUID,
    claim_generation: int,
    gateway: Any,
) -> str:
    """执行一个同步页或一个异步 task 阶段。

    异步任务号与完整 query 绑定在持久 run 上；超时或状态未就绪只安排后继
    唤醒，不在本调用内 sleep，也不会在不确定的 create 结果后紧循环重建任务。
    """
    if type(claim_generation) is not int or claim_generation < 1:
        raise DomainError("report_claim_invalid", "报表 claim 代数无效")
    run = _run(session, run_id)
    if run.claim_generation != claim_generation:
        return _failed(run, DomainError("report_claim_lost", "报表同步 claim 已被替换"))
    if run.status in {"COMPLETE", "FAILED", "CANCELLED", "STALE"}:
        return "FAILED" if run.status == "FAILED" else "READY"
    try:
        query = decode_query(run.query)
    except (TypeError, ValueError, KeyError):
        return _failed(run, DomainError("report_query_invalid", "报表查询合同无效"))
    reports = getattr(gateway, "reports", None)
    if reports is None:
        return _failed(run, DomainError("report_gateway_missing", "报表 gateway 未注册"))
    try:
        # task_status 是 A7/持久化调度选择异步通道的冻结信号；没有 task
        # 状态的运行使用同步分页，避免把 SDK async_req 误当平台任务。
        if run.task_status is not None:
            if run.task_id is None:
                task = reports.create_task(query)
                run.task_id = task.task_id
                return _wait(run, task_status=task.status)
            persisted_status = {
                "QUEUING": "PENDING",
                "PROCESSING": "RUNNING",
                "SUCCESS": "READY",
            }.get(run.task_status, run.task_status)
            task = ReportTask(
                run.task_id,
                query.advertiser_id,
                cast(Literal["PENDING", "RUNNING", "READY", "FAILED"], persisted_status),
                query,
            )
            checked = reports.check_task(task)
            run.task_status = checked.status
            if checked.status in {"PENDING", "RUNNING"}:
                return _wait(run, task_status=checked.status)
            if checked.status == "FAILED":
                return _failed(run, DomainError("report_task_failed", "平台异步报表失败"))
            page = reports.download_task(checked)
        else:
            page = reports.read_page(query)
        # A1 的完整文件语义要求 READY 下载固定为单页；同步页则继续按
        # 平台 page_info 推进，任何未知截断都只能进入失败/未知覆盖。
        if not page.complete and page.next_page is None:
            return _failed(run, DomainError("report_incomplete", "报表覆盖不完整"))
        stage_report_page(
            session,
            run_id=run.id,
            page=page,
            claim_generation=claim_generation,
        )
        if page.complete:
            publish_report(session, run_id=run.id, claim_generation=claim_generation)
            return "READY"
        run.next_page = page.next_page or run.next_page
        run.status = "RUNNING"
        run.coverage = "PENDING"
        run.observed_at = datetime.now(UTC)
        return "CONTINUE"
    except DomainError as error:
        # 旧发布、权限/契约变化、重复页和半文件都不能静默发布完整覆盖。
        return _failed(run, error)
    finally:
        session.flush()


__all__ = ["collect_report_step", "plan_report_shards"]
