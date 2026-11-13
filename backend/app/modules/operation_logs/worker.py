"""每小步一次物理读取；平台轮询等待落库并释放既有任务租约。"""

import hashlib
import io
import json
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlmodel import Session

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.change_log_reader import ChangeLogReader
from app.modules.operation_logs.parser import parse_export, utc_window
from app.modules.operation_logs.store import publish_export
from app.modules.reporting.publish import AccountMeta, _database_now
from app.modules.reporting.store_models import ReportSyncJob
from app.modules.sync_control.archive_storage import (
    ArchiveReceipt,
    ArchiveStorageError,
    S3RunArchive,
)


def run_step(
    session: Session,
    job: ReportSyncJob,
    reader: ChangeLogReader,
    accounts: dict[str, AccountMeta],
    *,
    external: Callable[[Callable[[], ArchiveReceipt]], ArchiveReceipt],
) -> int | None:
    if len(job.advertiser_ids) != 1:
        raise DomainError("operation_log_scope_invalid", "操作日志须逐账户同步")
    advertiser = job.advertiser_ids[0]
    timezone = accounts[advertiser].timezone
    state = dict(job.progress.get("operation_log", {}))
    now = _database_now(session)  # 观测次序以同一PG时钟为准，不比较不同Worker墙钟。
    phase = state.get("phase", "CREATE")
    if phase == "CREATE":
        attempts = state.get("create_attempts", 0) + 1
        if attempts > 3:
            raise DomainError(
                "operation_log_create_exhausted", "日志导出重建次数已达上限"
            )
        lower, upper = utc_window(job.start_date, job.end_date, timezone)
        # 最新窗口连同今日已发生的操作读取，连接最新实际配置锚点；
        # 今日不发布完整日覆盖，不能假称已取得尚未发生的操作。
        if (now - upper).total_seconds() < 86400 and upper <= now:
            upper = min(now.replace(microsecond=0), lower + timedelta(days=30))
        state.update(
            phase="CREATE",
            create_attempts=attempts,
            start_at=lower.isoformat(),
            end_at=upper.isoformat(),
            observed_at=now.isoformat(),
        )
        job.progress = job.progress | {"operation_log": dict(state)}
        task_id = reader.change_log_create(
            advertiser_id=advertiser, start=lower, end=upper
        )
        state.update(
            phase="CHECK",
            task_id=task_id,
            polls=0,
            task_ids=[*state.get("task_ids", []), task_id],
        )
    elif phase == "CHECK":
        polls = state.get("polls", 0) + 1
        if polls > 40:
            state.update(phase="CREATE")
        else:
            try:
                status = reader.change_log_check(
                    advertiser_id=advertiser, task_id=state["task_id"]
                )
            except DomainError as error:
                if error.code != "operation_log_task_unavailable":
                    raise
                state.update(phase="CREATE")
                # 新导出有独立的读取重试预算；重建总次数仍由create_attempts限制。
                job.attempts = 0
            else:
                state.update(
                    phase="DOWNLOAD" if status == "SUCCESS" else "CHECK", polls=polls
                )
    elif phase == "DOWNLOAD":
        try:
            content = reader.change_log_download(
                advertiser_id=advertiser, task_id=state["task_id"]
            )
        except DomainError as error:
            if error.code != "operation_log_task_unavailable":
                raise
            # 查询成功后也可能因排队过久失去文件；保留原窗口/审计，走有界重建。
            state.update(phase="CREATE")
            job.attempts = 0
            job.progress = job.progress | {"operation_log": dict(state)}
            job.due_at = now + timedelta(seconds=1)
            return None
        events = parse_export(content)
        payload = json.dumps({"changelog": content}, ensure_ascii=False).encode()
        content_digest = hashlib.sha256(payload).hexdigest()

        # 原始内容先进入既有私有审计桶；存储失败不可发布“完整覆盖”。
        def archive_content() -> ArchiveReceipt:
            from botocore.exceptions import (  # type: ignore[import-untyped]
                BotoCoreError,
                ClientError,
            )

            try:
                archive = S3RunArchive.configured()
                receipt = archive.persist(
                    tenant_id=job.tenant_id,
                    kind="report",
                    run_id=job.id,
                    payload=io.BytesIO(payload),
                    sha256_digest=content_digest,
                    size=len(payload),
                )
                archive.finalize(
                    tenant_id=job.tenant_id,
                    kind="report",
                    run_id=job.id,
                    receipt=receipt,
                )
                return receipt
            except ArchiveStorageError, BotoCoreError, ClientError, OSError:
                raise DomainError(
                    "operation_log_archive_unavailable",
                    "操作日志私有归档不可用",
                    retryable=True,
                ) from None

        receipt = external(archive_content)
        publish_export(
            session,
            job=job,
            content_digest=content_digest,
            receipt=receipt,
            events=events,
            start_at=datetime.fromisoformat(state["start_at"]),
            end_at=datetime.fromisoformat(state["end_at"]),
            timezone=timezone,
            observed_at=datetime.fromisoformat(state["observed_at"]),
        )
        state.update(phase="COMPLETE", digest=content_digest)
        job.progress = job.progress | {"operation_log": dict(state)}
        return len(events)
    else:
        raise DomainError("operation_log_phase_invalid", "操作日志任务阶段无效")
    job.progress = job.progress | {"operation_log": dict(state)}
    job.due_at = now + timedelta(seconds=30 if state["phase"] == "CHECK" else 1)
    return None
