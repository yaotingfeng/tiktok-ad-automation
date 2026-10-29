"""任务发布/开始/结束的无数据库诊断；所有关联值只作日志，绝不用于授权。"""

from datetime import UTC, datetime
from time import monotonic
from uuid import UUID

from celery.signals import (  # type: ignore[import-untyped]
    before_task_publish,
    task_postrun,
    task_prerun,
)

from app.core.diagnostics import emit, identifier, trace_context


@before_task_publish.connect(weak=False)
def stamp_publish(headers=None, **_kwargs):
    if isinstance(headers, dict):
        headers["diagnostic_published_at"] = datetime.now(UTC).timestamp()


@task_prerun.connect(weak=False)
def start_task(task_id=None, task=None, kwargs=None, **_kwargs):
    if task is None:
        return
    fields = {"task_id": identifier(task_id), "task_name": identifier(task.name)}
    arguments = kwargs if isinstance(kwargs, dict) else {}
    payload = arguments.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    for key in (
        "tenant_id",
        "submission_id",
        "unit_id",
        "step_id",
        "job_id",
        "operation_id",
    ):
        value = arguments.get(key) if key == "tenant_id" else payload.get(key)
        try:
            fields[key] = str(UUID(str(value)))
        except ValueError:
            continue
    request = task.request
    request._diagnostic_token = trace_context.set(fields)
    request._diagnostic_started = monotonic()
    headers = request.headers or {}
    now = datetime.now(UTC).timestamp()
    timing = {}
    for key, label in (
        ("diagnostic_published_at", "broker_wait_ms"),
        ("diagnostic_due_at", "due_to_start_ms"),
    ):
        value = headers.get(key)
        if (
            isinstance(value, float | int)
            and not isinstance(value, bool)
            and 0 < value <= now
        ):
            timing[label] = round((now - value) * 1000, 3)
    emit(
        "task_started",
        queue=identifier((request.delivery_info or {}).get("routing_key")),
        **timing,
    )


@task_postrun.connect(weak=False)
def finish_task(task=None, state=None, **_kwargs):
    if task is None:
        return
    request = task.request
    token = getattr(request, "_diagnostic_token", None)
    if token is None:
        return
    try:
        emit(
            "task_finished",
            celery_state=identifier(state),
            duration_ms=round((monotonic() - request._diagnostic_started) * 1000, 3),
        )
    finally:
        trace_context.reset(token)
        del request._diagnostic_token
