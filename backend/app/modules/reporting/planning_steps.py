"""有界规划事务：阶段游标与该批业务任务一起提交，失败不撤销其它阶段。"""

import copy
import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from billiard.exceptions import SoftTimeLimitExceeded
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session, select

from app.core.errors import DomainError
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.modules.reporting import planner
from app.modules.reporting.jobs import _CountingReader
from app.modules.reporting.query_budget import QueryBudgetExceeded, query_budget
from app.modules.reporting.store_models import AdSyncBc

logger = logging.getLogger(__name__)
KEY = "planning_steps_v1"
PHASES = (
    "screen_hot",
    "hot",
    "material",
    "empty_hot",
    "closeout",
    "screen_daily",
    "daily",
    "empty_daily",
    "history",
    "directory",
)
# 每个筛查/后台批之间给两对实时事实批；无实时工作时跳过空位，后台继续用预算。
PHASE_ORDER = tuple(
    item
    for phase in PHASES
    if phase not in {"hot", "material"}
    for item in (phase, "hot", "material", "hot", "material")
)
BATCH = 64
SECONDS = 3


class ScreenYield(Exception):
    """一个平台页已保存，下次从下一页继续；调用方提交而非回滚。"""


def _new(now, **values):
    return {
        "at": now.isoformat(),
        "after": "",
        "size": BATCH,
        "deferred": {},
        "done": False,
        **values,
    }


def scope_digest(plan, accounts, timezones):
    return hashlib.sha256(
        json.dumps(
            [
                str(plan.actor_id),
                plan.frozen_route,
                [(a, timezones.get(a)) for a in accounts],
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()


def schedule_scope_changes(session, plan):
    state = plan.screening_progress.get(KEY)
    if state:
        accounts = planner.owned_accounts(session, plan)
        timezones = planner._timezones(session, plan.tenant_id, accounts)
        if state.get("scope") != scope_digest(plan, accounts, timezones):
            # 新账户/时区/授权不等待下一周期；下一事务按新范围重建游标，保留规则相位。
            plan.screening_progress = {
                **plan.screening_progress,
                KEY: {**state, "pending": True},
            }


def _prepare(plan, accounts, now, timezones):
    scope = scope_digest(plan, accounts, timezones)
    state: dict[str, Any] = copy.deepcopy(plan.screening_progress.get(KEY) or {})
    if state.get("scope") != scope:
        # 新授权/归属不能沿用旧范围的完成游标。旧已提交任务仍保留其冻结身份。
        changed = bool(state)
        state = {"scope": scope, "phase": 0}
        if changed:
            state["force_live"] = True
            plan.next_daily_at = min(plan.next_daily_at, now)
            plan.next_directory_at = min(plan.next_directory_at, now)
    live = state.get("live")
    if (not live or live.get("done")) and (
        state.get("force_live") or min(plan.next_hot_at, plan.next_material_at) <= now
    ):
        forced = state.pop("force_live", False)
        hot_due = plan.next_hot_at <= now or forced
        state["live"] = {
            "anchor": plan.next_hot_at.isoformat(),
            "at": now.isoformat(),
            "hot_due": hot_due,
        }
        state["hot"] = _new(now, done=not hot_due)
        state["material"] = _new(now)
        if hot_due:
            if state.get("screen_hot", {}).get("done", True):
                state["screen_hot"] = _new(now)
            if state.get("closeout", {}).get("done", True):
                state["closeout"] = _new(now)
        if state.get("history", {}).get("done", True):
            state["history"] = _new(now)
    if (
        plan.next_daily_at <= now
        and state.get("screen_daily", {}).get("done", True)
        and "daily" not in state
    ):
        state["screen_daily"] = _new(now)
    return state


def _save(plan, state):
    state["pending"] = (
        any(
            isinstance(state.get(p), dict) and not state[p].get("done")
            for p in PHASES
            if p != "directory"
        )
        or bool(plan.screening_progress.get("directory_cycle_v1"))
        or bool(state.get("metadata_missing"))
        or bool(state.get("force_live"))
    )
    plan.screening_progress = {**plan.screening_progress, KEY: copy.deepcopy(state)}


def _batch(step, accounts, now):
    deferred = step.setdefault("deferred", {})
    retry = sorted(
        a
        for a, at in deferred.items()
        if a in accounts and datetime.fromisoformat(at) <= now
    )
    fresh = [a for a in accounts if a > step["after"] and a not in deferred]
    return (retry + fresh)[: max(1, step.get("size", BATCH))]


def _advance_batch(step, batch, accounts):
    deferred = step["deferred"]
    fresh = [a for a in batch if a not in deferred]
    if fresh:
        step["after"] = max(step["after"], *fresh)
    for account in batch:
        deferred.pop(account, None)
    step["size"] = min(BATCH, step.get("size", BATCH) * 2)
    step["done"] = not deferred and not any(a > step["after"] for a in accounts)


def _eligible(state, phase, plan, now, allow_backfill, accounts):
    if (
        phase in {"screen_daily", "daily", "empty_daily", "history"}
        and not allow_backfill
    ):
        return False
    if phase == "directory":
        if (
            state.get("directory_retry")
            and datetime.fromisoformat(state["directory_retry"]) > now
        ):
            return False
        cursor = plan.screening_progress.get("directory_cycle_v1")
        return plan.next_directory_at <= now or bool(
            cursor
            and (
                any(
                    a > cursor["after"] and a not in cursor.get("deferred", {})
                    for a in accounts
                )
                or any(
                    datetime.fromisoformat(at) <= now
                    for at in cursor.get("deferred", {}).values()
                )
            )
        )
    step = state.get(phase)
    return bool(
        step
        and not step.get("done")
        and datetime.fromisoformat(step.get("retry_at", step["at"])) <= now
        and (
            phase.startswith("screen_")
            or _batch(step, accounts, now)
            or not step.get("deferred")
        )
    )


def _execute(
    session, plan, state, phase, accounts, now, database_engine, redis_client, budget
):
    from app.modules.reporting import job_tasks

    if phase == "directory":
        before = plan.screening_progress.get("directory_cycle_v1", {}).get("after", "")
        job_tasks._plan_directory_step(session, plan, accounts=accounts, now=now)
        cursor = plan.screening_progress.get("directory_cycle_v1")
        if cursor and cursor["after"] == before:
            state["directory_retry"] = plan.next_directory_at.isoformat()
        return
    step = state[phase]
    at = datetime.fromisoformat(step["at"])
    if phase.startswith("screen_"):
        days = 1 if phase == "screen_hot" else 32
        counted = None
        try:
            with job_tasks._reader_for(
                database_engine=database_engine,
                redis_client=redis_client,
                plan=plan,
                deadline=datetime.now(UTC) + timedelta(seconds=budget),
            ) as reader:
                if reader is None:
                    plan.enabled, plan.disabled_reason = False, "api_binding_required"
                    return
                counted = _CountingReader(reader)
                try:
                    active = job_tasks._active(
                        session,
                        counted,
                        plan,
                        accounts,
                        at,
                        days=days,
                        single_step=True,
                    )
                except ScreenYield:
                    return  # 已保存页游标，必须正常退出SAVEPOINT以便提交。
            step["done"] = True
            if days == 1:
                if state.get("metadata_missing"):
                    plan.active_screened_at = None
                plan.last_active_advertiser_ids = active
                state["empty_hot"] = _new(at, active=active)
                # 分页筛查可能晚于本轮事实批次：重新覆盖已走过的账户，不丢新发现活跃对象。
                for name in ("hot", "material"):
                    if state.get(name, {}).get("after"):
                        state[name] = _new(
                            datetime.fromisoformat(state["live"]["at"]),
                            covered=state[name].get("covered", []),
                        )
                if state.get("live", {}).get("done"):
                    state["live"]["done"] = False
            else:
                state["daily"] = _new(at, active=active)
                state["empty_daily"] = _new(at, active=active)
        finally:
            if counted:
                planner.record_calls(redis_client, now, counted.count)
        return
    batch = _batch(step, accounts, now)
    if not batch:
        if not step["deferred"]:
            step["done"] = True
        return
    if phase in {"hot", "material", "empty_hot"}:
        active = set(
            plan.last_active_advertiser_ids
        ) | planner.known_reporting_accounts(session, plan, accounts=batch, now=at)
        covered = set(step.get("covered", []))
        requested = sorted(active.intersection(batch) - covered)
        if phase == "hot":
            planner.plan_hot(session, plan, active=requested, now=at)
            step["covered"] = sorted(covered | set(requested))
        elif phase == "material":
            planner.plan_material(session, plan, active=requested, now=at)
            step["covered"] = sorted(covered | set(requested))
        else:
            active.update(step["active"])
            planner.mark_confirmed_empty(
                session,
                plan,
                inactive=sorted(set(batch) - active),
                window_fn=lambda tz: planner._hot_window(tz, at),
                now=at,
                observed_at=at,
            )
    elif phase == "closeout":
        planner.plan_day_closeout(session, plan, now=at, accounts=batch)
    elif phase == "daily":
        planner.plan_daily(
            session,
            plan,
            active_32d=sorted(set(batch).intersection(step["active"])),
            now=at,
        )
    elif phase == "empty_daily":
        planner.mark_confirmed_empty(
            session,
            plan,
            inactive=sorted(set(batch) - set(step["active"])),
            window_fn=planner.daily_window(at),
            now=at,
            observed_at=at,
        )
    elif phase == "history":
        ranges = step.setdefault("ranges", {})
        planner.plan_history(session, plan, now=at, accounts=batch, progress=ranges)
        for account in batch:
            if account in ranges:
                step["deferred"][account] = now.isoformat()
        batch = [a for a in batch if a not in ranges]
    _advance_batch(step, batch, accounts)
    if phase == "closeout" and step.get("done") and step["at"] != state["live"]["at"]:
        state[phase] = _new(datetime.fromisoformat(state["live"]["at"]))


def plan_step(database_engine, redis_client, key, *, now, allow_backfill, budget=8):
    """最多一个阶段事务；返回是否前进，空转阶段不会消耗后续BC的时间预算。"""
    from app.modules.reporting import job_tasks

    with Session(database_engine) as session, session.begin():
        plan = session.exec(
            select(AdSyncBc)
            .where(AdSyncBc.tenant_id == key[0], AdSyncBc.bc_id == key[1])
            .with_for_update(skip_locked=True)
        ).one_or_none()
        if plan is None or not plan.enabled:
            return False
        previous = plan.screening_progress.get(KEY, {})
        if (
            previous.get("retry_at")
            and datetime.fromisoformat(previous["retry_at"]) > now
        ):
            _save(plan, {**previous, "last_turn": datetime.now(UTC).isoformat()})
            return False
        accounts = planner.owned_accounts(session, plan)
        if not accounts:
            return False
        timezones = planner._timezones(session, plan.tenant_id, accounts)
        state = _prepare(plan, accounts, now, timezones)
        missing = []
        for account in accounts:
            try:
                ZoneInfo(timezones.get(account, ""))
            except ZoneInfoNotFoundError, ValueError, TypeError:
                missing.append(account)
        state["metadata_missing"] = missing
        if missing:
            plan.active_screened_at = None
        missing_set = set(missing)
        valid_accounts = [a for a in accounts if a not in missing_set]
        phase = None
        for offset in range(len(PHASE_ORDER)):
            index = (state["phase"] + offset) % len(PHASE_ORDER)
            if _eligible(
                state,
                PHASE_ORDER[index],
                plan,
                now,
                allow_backfill,
                accounts if PHASE_ORDER[index] == "directory" else valid_accounts,
            ):
                phase = PHASE_ORDER[index]
                state["phase"] = (index + 1) % len(PHASE_ORDER)
                break
        if phase is None:
            state["last_turn"] = datetime.now(UTC).isoformat()
            _save(plan, state)
            return False
        before = copy.deepcopy(state)
        budget = min(budget, 8 if phase.startswith("screen_") else SECONDS)
        try:
            with session.begin_nested(), query_budget(session, seconds=budget):
                _execute(
                    session,
                    plan,
                    state,
                    phase,
                    accounts if phase == "directory" else valid_accounts,
                    now,
                    database_engine,
                    redis_client,
                    budget,
                )
                _save(plan, state)
                session.flush()
        except (
            DBAPIError,
            QueryBudgetExceeded,
            DomainError,
            AccountAdmissionDeferred,
        ) as error:
            # 只有当前批次回滚；保存缩批/重试与阶段轮转，慢阶段不会挡住其他工作。
            state = before
            delay = timedelta(seconds=5)
            if isinstance(error, DBAPIError) and getattr(
                error.orig, "sqlstate", None
            ) not in {"57014", "55P03"}:
                raise
            if isinstance(error, AccountAdmissionDeferred):
                delay = timedelta(milliseconds=max(1000, error.retry_after_ms))
            elif isinstance(error, DomainError):
                if (
                    error.code == "report_permission_denied"
                    or error.code == "tiktok_response_error"
                    and any(v in error.message.lower() for v in ("permission", "权限"))
                ):
                    plan.bc_report_supported = False
                    delay = timedelta(minutes=1)
                elif error.code not in {
                    "read_deadline_exceeded",
                    "tiktok_call_deadline_exceeded",
                }:
                    plan.disabled_reason = error.code
                    delay = timedelta(minutes=5)
            if phase == "directory":
                plan.next_directory_at = now + delay
                state["directory_retry"] = (now + delay).isoformat()
            else:
                step = state[phase]
                step["retry_at"] = (now + delay).isoformat()
                if phase == "screen_daily":
                    plan.next_daily_at = now + delay
                if not phase.startswith("screen_"):
                    batch = _batch(step, valid_accounts, now)
                    step["size"] = max(1, step.get("size", BATCH) // 2)
                    if len(batch) == 1:
                        step["deferred"][batch[0]] = (
                            now + timedelta(minutes=5)
                        ).isoformat()
            logger.warning(
                "report planning phase deferred: bc=%s phase=%s error=%s",
                plan.bc_id,
                phase,
                type(error).__name__,
            )
        # 时钟只在广告和素材都完整规划后推进；相位锚点不使用失败重试时刻。
        if (
            state.get("live")
            and not state["live"].get("done")
            and all(state.get(p, {}).get("done") for p in ("hot", "material"))
        ):
            if not state["live"].get("clock_advanced"):
                # 规则相位可能在批次之间被调整，不能用旧冻结anchor覆盖当前配置。
                anchor = plan.next_hot_at
                plan.next_hot_at = plan.next_material_at = (
                    job_tasks._advance(anchor, now, planner.HOT_INTERVAL)
                    if state["live"]["hot_due"] and anchor <= now
                    else anchor
                )
                state["live"]["clock_advanced"] = True
            at = datetime.fromisoformat(state["live"]["at"])
            if at != now and any(
                planner.local_today(tz, at) != planner.local_today(tz, now)
                for tz in {timezones[a] for a in valid_accounts}
            ):
                # 跨当地午夜的旧轮先完整提交，再立即发现新当地日，不能跳到15分钟后。
                state["force_live"] = True
            state["live"]["done"] = True
        if "daily" in state and all(
            state.get(p, {}).get("done") for p in ("daily", "empty_daily")
        ):
            daily_at = datetime.fromisoformat(state["daily"]["at"])
            crossed_day = any(
                planner.local_today(tz, daily_at) != planner.local_today(tz, now)
                for tz in {timezones[a] for a in valid_accounts}
            )
            # 冻结窗口跨日完成时还有刚结束的一天未发现，先追赶再恢复每日时钟。
            plan.next_daily_at = now if crossed_day else planner._next_daily(now)
            del state["daily"]
            del state["empty_daily"]
        state["last_turn"] = datetime.now(UTC).isoformat()
        _save(plan, state)
        return True


def run_plan(
    database_engine,
    redis_client,
    key,
    *,
    now,
    deadline,
    allow_backfill,
    one_step=False,
    time_budget=None,
):
    # 测试/恢复可传历史逻辑时钟；运行预算始终使用单调时钟而非业务日期。
    remaining = (deadline - now).total_seconds() if time_budget is None else time_budget
    stop = monotonic() + max(0, min(10, remaining))
    advanced = False
    while monotonic() < stop:
        try:
            worked = plan_step(
                database_engine,
                redis_client,
                key,
                now=now,
                allow_backfill=allow_backfill,
                budget=max(0.05, stop - monotonic()),
            )
        except SoftTimeLimitExceeded:
            # 当前事务已退出并回滚；之前各批次仍已提交，下轮继续持久游标。
            raise
        advanced |= worked
        if one_step or not worked:
            break
    return advanced
