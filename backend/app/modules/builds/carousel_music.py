"""草稿配乐：冻结来源、独立音乐版本、短事务与真实 SDK 读取。"""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlmodel import Session, col, select

from app.core.errors import DomainError
from app.integrations.tiktok.contracts.context import FrozenTikTokRoute
from app.integrations.tiktok.contracts.materials import RemoteCallBudget
from app.integrations.tiktok.gateway import open_tiktok_gateway
from app.integrations.tiktok.sdk import AccountAdmissionDeferred
from app.jobs.admission import admission_policy
from app.modules.accounts.routing import freeze_route, verify_route
from app.modules.materials.image_distribution import image_ready, prepare_image_targets
from app.modules.materials.ingest_models import IngestSessionFile
from app.modules.materials.models import AccountMaterial
from app.modules.tenants.permissions import require_tenant

from .carousel_music_models import MusicSelectionJob
from .image_composition import effective_music_rule, resolve_image_rule
from .image_composition_models import DraftCreativeUnit, DraftCreativeUnitItem
from .image_composition_schemas import ImageRule
from .image_composition_service import (
    ImageDraftScope,
    _draft,
    _editable,
    _receipt,
    _state,
    digest,
)
from .models import BuildDraft


def manual_selection(unit, music_id):
    """两个手工入口共用同一栅栏；选曲事实不代表目标账户投放资格。"""
    return {
        "music_id": music_id,
        "music_protected": True,
        "music_status": "READY",
        "music_revision": unit.music_revision + 1,
        "music_request_generation": unit.music_request_generation + 1,
    }


def _authority(session, scope, account=None, *, write=True, lock=False):
    if write:
        require_tenant(
            session, tenant_id=scope.tenant_id, actor_id=scope.actor_id, action="build"
        )
    draft = _draft(session, scope, lock=lock)
    current_route = freeze_route(
        session,
        context=scope.context,
        bc_id=draft.bc_id,
        connection_id=draft.execution_connection_id,
    )
    if current_route != scope.route:
        raise DomainError("route_binding_changed", "草稿的执行连接已改变")
    if scope.route.channel != "OFFICIAL_API":
        raise DomainError("music_channel_unsupported", "当前连接不支持音乐读取")
    if account:
        verify_route(
            session,
            context=scope.context,
            route=scope.route,
            advertiser_id=account,
            capability="build" if write else "read",
        )
    return draft


def _seed(session, scope, *, write=False):
    # 来源必须是本 BC/连接下实际记录过的账户；目标广告账户不是素材来源证明。
    candidates = session.exec(
        select(AccountMaterial.advertiser_id)
        .where(
            AccountMaterial.tenant_id == scope.tenant_id,
            AccountMaterial.bc_id == scope.bc_id,
            AccountMaterial.connection_id == scope.route.connection_id,
            AccountMaterial.media_type == "IMAGE",
            AccountMaterial.status == "available",
        )
        .distinct()
        .order_by(col(AccountMaterial.advertiser_id))
    ).all()
    candidates += session.exec(
        select(IngestSessionFile.source_advertiser_id)
        .where(
            IngestSessionFile.tenant_id == scope.tenant_id,
            IngestSessionFile.bc_id == scope.bc_id,
            IngestSessionFile.connection_id == scope.route.connection_id,
            col(IngestSessionFile.source_advertiser_id).is_not(None),
        )
        .distinct()
        .order_by(col(IngestSessionFile.source_advertiser_id))
    ).all()
    for account in dict.fromkeys(candidates):
        if not account:
            continue
        try:
            verify_route(
                session,
                context=scope.context,
                route=scope.route,
                advertiser_id=account,
                capability="build" if write else "read",
            )
            return account
        except DomainError:
            continue
    return None


def _items(session, unit):
    return session.exec(
        select(DraftCreativeUnitItem.material_id)
        .where(
            DraftCreativeUnitItem.tenant_id == unit.tenant_id,
            DraftCreativeUnitItem.draft_id == unit.draft_id,
            DraftCreativeUnitItem.generation == unit.generation,
            DraftCreativeUnitItem.unit_id == unit.id,
        )
        .order_by(col(DraftCreativeUnitItem.position))
    ).all()


def _target(unit):
    return {
        "unit_id": str(unit.id),
        "revision": unit.revision,
        "content_digest": unit.content_digest,
        "rule_digest": unit.music_rule_digest,
        "request_generation": unit.music_request_generation,
    }


def _matches(unit, target):
    return unit is not None and _target(unit) == target and not unit.music_protected


def _effective_parameters(state, unit, image_count):
    rule = resolve_image_rule(
        ImageRule.model_validate(state.rule or {}),
        state.drama_patches.get(str(unit.drama_id), {}),
    )
    return effective_music_rule(rule, image_count)


def enqueue_music(session, scope, draft, *, unit_ids=None):
    """与激活/编辑同事务入队；不展开目标账户，不在事务里访问平台。"""
    from .carousel_music_tasks import queue_music

    session.flush()
    state = _state(session, draft)
    query = (
        select(DraftCreativeUnit)
        .where(
            DraftCreativeUnit.tenant_id == scope.tenant_id,
            DraftCreativeUnit.draft_id == draft.id,
            DraftCreativeUnit.generation == draft.active_image_generation,
            col(DraftCreativeUnit.music_status).in_(["PENDING", "RUNNING"]),
            col(DraftCreativeUnit.music_protected).is_(False),
        )
        .order_by(col(DraftCreativeUnit.id))
    )
    if unit_ids is not None:
        query = query.where(col(DraftCreativeUnit.id).in_(unit_ids))
    units = session.exec(query).all()
    if not units:
        return []
    if len(units) > 20:
        raise ValueError("music preparation slice exceeds 20 units")
    seed = _seed(session, scope, write=True)
    result = []
    for unit in units:
        ids = _items(session, unit)
        intent = unit.music_snapshot.get("pending_request", {})
        # 显式请求只隔离请求身份，不获得覆盖 FIXED/SINGLE 规则的权限。
        parameters = intent.get("parameters") or _effective_parameters(
            state, unit, len(ids)
        )
        if intent:
            # 结构重生成继承该快照；RUNNING 不应丢失用户已经接受的显式请求。
            unit.music_snapshot = {
                **unit.music_snapshot,
                "pending_request": {**intent, "parameters": parameters},
            }
        if parameters["mode"] != "RECOMMEND" and not parameters.get("music_id"):
            unit.music_status = "NEEDS_SELECTION"
            continue
        fingerprint = digest(
            {
                "route": scope.route.model_dump(mode="json"),
                "seed": seed,
                "content": unit.content_digest,
                "parameters": parameters,
                "nonce": intent.get("request_id"),
            }
        )
        job = session.exec(
            select(MusicSelectionJob).where(
                MusicSelectionJob.tenant_id == scope.tenant_id,
                MusicSelectionJob.draft_id == draft.id,
                MusicSelectionJob.active_generation == draft.active_image_generation,
                MusicSelectionJob.fingerprint == fingerprint,
            )
        ).first()
        target = _target(unit)
        if job is None:
            job = MusicSelectionJob(
                tenant_id=scope.tenant_id,
                draft_id=draft.id,
                bc_id=scope.bc_id,
                actor_id=scope.actor_id,
                active_generation=draft.active_image_generation,
                fingerprint=fingerprint,
                route=scope.route.model_dump(mode="json"),
                seed_advertiser_id=seed,
                parameters=parameters,
                material_ids=list(map(str, ids)),
                targets=[target],
            )
            session.add(job)
            session.flush()
            queue_music(session, job)
        elif target not in job.targets:
            job.targets = [*job.targets, target]
            if job.status in {"READY", "FAILED", "CANCELLED"}:
                job.status = "PENDING"
                job.generation += 1
                queue_music(session, job)
        result.append(job.id)
    return list(dict.fromkeys(result))


def request_unit_recommendation(session, scope, draft, unit, request_id, action):
    """共享单元转换：不推进草稿版本，供用户单项与已冻结批量分片调用。"""
    effective = _effective_parameters(
        _state(session, draft), unit, len(_items(session, unit))
    )
    if action == "RECOMMEND" and effective["mode"] == "FIXED":
        # 必须在改版本/保护/状态之前拒绝；手工指定仍走 SET_MANUAL。
        raise DomainError(
            "music_request_invalid",
            "指定曲模式不支持重新推荐，请手工选择曲目或修改配乐规则",
        )
    if action == "RETRY" and unit.music_status != "FAILED":
        raise DomainError("music_request_invalid", "仅失败项可以重试")
    unit.music_request_generation += 1
    unit.music_revision += 1
    unit.music_protected = False
    unit.music_status = "PENDING"
    old_parameters = (
        unit.music_snapshot.get("pending_request", {}).get("parameters")
        if action == "RETRY"
        else None
    )
    if effective["mode"] == "FIXED":
        # 失败重试仍精确读取当前配置曲，不能恢复成推荐请求。
        old_parameters = effective
    unit.music_snapshot = {
        **unit.music_snapshot,
        "pending_request": {
            "request_id": str(request_id),
            "action": action,
            "parameters": old_parameters,
        },
    }
    jobs = enqueue_music(
        session,
        scope,
        draft,
        unit_ids=[unit.id],
    )
    return jobs


def request_music(
    session,
    scope,
    unit_id,
    expected_content_revision,
    expected_music_revision,
    request_id,
    action,
    music_id=None,
):
    # 与 T2 共用请求锁域和锁顺序，防止跨草稿复用 request_id 落成数据库异常。
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"image-request:{scope.tenant_id}:{request_id}"},
    )
    draft = _authority(session, scope, lock=True)
    _editable(session, draft)
    body = {
        "unit_id": str(unit_id),
        "content_revision": expected_content_revision,
        "music_revision": expected_music_revision,
        "action": action,
        "music_id": music_id,
    }
    from .image_composition_state import ImageCompositionRequest

    fp = digest({"draft_id": scope.draft_id, "operation": "music", "body": body})
    prior = session.get(ImageCompositionRequest, (scope.tenant_id, request_id))
    if prior:
        if prior.draft_id != scope.draft_id or prior.digest != fp:
            raise DomainError("idempotency_conflict", "请求标识已用于不同参数")
        return prior.result
    unit = session.get(
        DraftCreativeUnit,
        (scope.tenant_id, draft.id, draft.active_image_generation, unit_id),
    )
    if unit is None:
        raise DomainError("image_composition_not_found", "创意不存在")
    if (unit.revision, unit.music_revision) != (
        expected_content_revision,
        expected_music_revision,
    ):
        raise DomainError("draft_revision_conflict", "创意或音乐已变化，请刷新")
    if action == "SET_MANUAL":
        if (
            not music_id
            or not music_id.strip()
            or music_id != music_id.strip()
            or len(music_id) > 128
        ):
            raise DomainError("music_request_invalid", "曲目标识无效")
        for key, value in manual_selection(unit, music_id).items():
            setattr(unit, key, value)
        unit.music_snapshot = {"selection_source": "MANUAL"}
        jobs = []
    elif action in {"RECOMMEND", "RETRY"}:
        jobs = request_unit_recommendation(
            session, scope, draft, unit, request_id, action
        )
    else:
        raise DomainError("music_request_invalid", "配乐动作无效")
    # 用户意图使旧计划失效；后台完成不增加草稿版本。
    was_applied = draft.applied_image_rule_revision == draft.revision
    from .drafts import _bump_revision

    _bump_revision(session, draft, draft.revision)
    if was_applied:
        draft.applied_image_rule_revision = draft.revision
    return _receipt(
        session,
        scope,
        request_id,
        fp,
        {
            "job_ids": list(map(str, jobs)),
            "revision": draft.revision,
            "music_revision": unit.music_revision,
            "request_generation": unit.music_request_generation,
            "status": unit.music_status,
        },
    )


def _scope(job):
    return ImageDraftScope(
        job.tenant_id,
        job.bc_id,
        job.draft_id,
        job.actor_id,
        FrozenTikTokRoute.model_validate(job.route),
    )


def _locked(session, identity):
    job = session.get(MusicSelectionJob, identity)
    if job is None:
        raise DomainError("image_composition_not_found", "配乐任务不存在")
    session.exec(
        select(BuildDraft).where(BuildDraft.id == job.draft_id).with_for_update()
    ).one()
    return session.exec(
        select(MusicSelectionJob)
        .where(MusicSelectionJob.id == identity)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()


def _public(job):
    return {
        "job_id": str(job.id),
        "generation": job.generation,
        "status": job.status,
        "processed": job.cursor,
        "total": len(job.targets),
        "attempts": job.attempts,
        "error_code": job.error_code,
    }


def _publish(session, job, scope):
    draft = _authority(session, scope, job.seed_advertiser_id, lock=True)
    _publish_result(session, job, draft)


def _publish_result(session, job, draft):
    if (
        draft.tenant_id != job.tenant_id
        or draft.bc_id != job.bc_id
        or draft.active_image_generation != job.active_generation
    ):
        job.status, job.claim, job.repair_after = "CANCELLED", None, None
        return
    # 每片最多 20 个订阅者；跨 generation 只保留审计，不直接写新单元。
    for target in job.targets[job.cursor : job.cursor + 20]:
        unit = session.get(
            DraftCreativeUnit,
            (
                job.tenant_id,
                job.draft_id,
                job.active_generation,
                UUID(target["unit_id"]),
            ),
        )
        if _matches(unit, target):
            unit.music_revision += 1
            unit.music_status = job.result.get("status", "FAILED")
            if unit.music_status == "READY":
                unit.music_id = job.result["music_id"]
                unit.music_snapshot = dict(job.result["snapshot"])
            else:
                unit.music_snapshot = {
                    **unit.music_snapshot,
                    "error_code": job.error_code,
                }
        job.cursor += 1
    if job.cursor == len(job.targets):
        job.status = "READY" if job.result.get("status") == "READY" else "FAILED"
        job.repair_after = None
    else:
        from .carousel_music_tasks import queue_music

        job.status = "PENDING"
        job.generation += 1
        queue_music(session, job)
    job.claim = None


def _fail_music(session, job, error_code):
    """撤权只能阻止业务成功，不能留下永不终结的本地配乐状态。

    调用方已通过_locked持有草稿/任务锁；仅写失败状态，不读取平台或写入曲目。
    复用发布的代次、内容、请求及手工保护检查，并继续按20个订阅者分片收尾。
    """
    job.error_code, job.result = error_code, {"status": "FAILED"}
    draft = session.get(BuildDraft, job.draft_id)
    assert draft is not None  # _locked 已锁定并确认草稿存在。
    _publish_result(session, job, draft)


def _skip_obsolete_subscribers(session, job):
    """手工选曲只取消自己的订阅；不能让首片失效污染后续仍有效的重复组合。"""
    targets = job.targets[job.cursor : job.cursor + 20]
    if any(
        _matches(
            session.get(
                DraftCreativeUnit,
                (
                    job.tenant_id,
                    job.draft_id,
                    job.active_generation,
                    UUID(target["unit_id"]),
                ),
            ),
            target,
        )
        for target in targets
    ):
        return False
    job.cursor += len(targets)
    job.claim = None
    if job.cursor == len(job.targets):
        job.status, job.repair_after = "CANCELLED", None
    else:
        from .carousel_music_tasks import queue_music

        job.status = "PENDING"
        job.generation += 1
        queue_music(session, job)
    return True


def _budget(deadline, route, operation):
    return RemoteCallBudget(
        deadline, 45, admission_policy(operation, channel=route.channel).lease_ms
    )


def advance_music(job_id, generation, *, database_engine, redis_client):
    """一次任务最多一次推荐/精确读取；网络期间不持有业务事务或行锁。"""
    deadline = datetime.now(UTC) + timedelta(seconds=10)
    with Session(database_engine) as session, session.begin():
        job = _locked(session, job_id)
        if job.generation != generation or job.status in {
            "READY",
            "FAILED",
            "CANCELLED",
        }:
            return _public(job)
        if job.claim and job.repair_after and job.repair_after > datetime.now(UTC):
            return _public(job)
        scope = _scope(job)
        try:
            draft = _authority(session, scope, job.seed_advertiser_id, lock=True)
            if draft.active_image_generation != job.active_generation:
                job.status = "CANCELLED"
                return _public(job)
            if not job.result:
                # 执行键含 generation，内容指纹不含；旧代结果仅作为不可变候选重新核验。
                cached = session.exec(
                    select(MusicSelectionJob)
                    .where(
                        MusicSelectionJob.tenant_id == job.tenant_id,
                        MusicSelectionJob.draft_id == job.draft_id,
                        MusicSelectionJob.fingerprint == job.fingerprint,
                        MusicSelectionJob.id != job.id,
                        col(MusicSelectionJob.result)["status"].astext == "READY",
                    )
                    .order_by(col(MusicSelectionJob.active_generation).desc())
                    .limit(1)
                ).first()
                if cached:
                    job.result = dict(cached.result)
            if job.result:
                _publish(session, job, scope)
                return _public(job)
            if _skip_obsolete_subscribers(session, job):
                return _public(job)
            if not job.seed_advertiser_id:
                raise DomainError(
                    "image_source_unavailable", "没有已记录且授权的图片种子账户"
                )
            if job.attempts >= 3:
                raise DomainError(
                    "music_retry_exhausted", "配乐重试次数已用完，请手工重试"
                )
            image_ids = []
            if job.parameters["mode"] == "RECOMMEND":
                targets = prepare_image_targets(
                    scope.route,
                    tuple(map(UUID, job.material_ids)),
                    (job.seed_advertiser_id,),
                    session=session,
                    context=scope.context,
                )
                for target in targets.items:
                    asset = (
                        session.get(AccountMaterial, target.asset_id)
                        if target.asset_id
                        else None
                    )
                    if not image_ready(asset, scope.route):
                        if (
                            target.state in {"blocked", "failed"}
                            or job.wait_turns >= 60
                        ):
                            raise DomainError(
                                "image_source_unavailable", "图片准备未完成或不可用"
                            )
                        from .carousel_music_tasks import queue_music

                        job.wait_turns += 1
                        job.generation += 1
                        queue_music(session, job, delay=30)
                        return _public(job)
                    assert asset is not None and asset.image_id is not None
                    image_ids.append(asset.image_id)
            for target in job.targets[job.cursor : job.cursor + 20]:
                unit = session.get(
                    DraftCreativeUnit,
                    (
                        job.tenant_id,
                        job.draft_id,
                        job.active_generation,
                        UUID(target["unit_id"]),
                    ),
                )
                if unit is not None and _matches(unit, target):
                    unit.music_status = "RUNNING"
                    unit.music_revision += 1
            job.claim, job.status = uuid4(), "RUNNING"
            job.attempts += 1
            job.repair_after = datetime.now(UTC) + timedelta(seconds=120)
            claim, account, params = (
                job.claim,
                job.seed_advertiser_id,
                dict(job.parameters),
            )
        except DomainError as error:
            _fail_music(session, job, error.code)
            return _public(job)
    # 真实图片 URL 仅在本次调用栈，不写数据库、outbox 或错误证据。
    selected, error_code, retry, retry_after = None, None, False, 0

    def before_remote():
        with Session(database_engine) as check:
            current = check.get(MusicSelectionJob, job_id)
            draft = _authority(check, scope, account)
            if (
                current is None
                or current.claim != claim
                or current.generation != generation
                or draft.active_image_generation != current.active_generation
            ):
                raise DomainError("music_request_superseded", "配乐请求已被替换")
            targets = current.targets[current.cursor : current.cursor + 20]
            if not any(
                _matches(
                    check.get(
                        DraftCreativeUnit,
                        (
                            current.tenant_id,
                            current.draft_id,
                            current.active_generation,
                            UUID(target["unit_id"]),
                        ),
                    ),
                    target,
                )
                for target in targets
            ):
                raise DomainError("music_request_superseded", "配乐请求已被替换")

    try:
        with open_tiktok_gateway(
            database_engine=database_engine,
            redis_client=redis_client,
            context=scope.context,
            route=scope.route,
            task_deadline=deadline,
            before_request=before_remote,
        ) as gateway:
            if params["mode"] == "RECOMMEND":
                urls = tuple(
                    gateway.materials.read_image_preview(
                        advertiser_id=account,
                        image_id=i,
                        budget=_budget(deadline, scope.route, "materials.get_images"),
                    ).image_url
                    for i in image_ids
                )
                page = gateway.music.recommend(
                    advertiser_id=account,
                    image_urls=urls,
                    budget=_budget(deadline, scope.route, "music.get"),
                )
                selected = next(
                    (
                        i
                        for i in page.items
                        if i.music_id and i.duration is not None and i.duration > 0
                    ),
                    None,
                )
                if selected is None:
                    error_code = "music_selection_required"
            else:
                page = gateway.music.read(
                    advertiser_id=account,
                    music_ids=(params["music_id"],),
                    budget=_budget(deadline, scope.route, "music.get"),
                )
                selected = next(
                    (i for i in page.items if i.music_id == params["music_id"]), None
                )
                if selected is None:
                    error_code = "music_not_found"
    except AccountAdmissionDeferred as error:
        error_code, retry = "music_admission_deferred", True
        retry_after = error.retry_after_ms / 1000
    except DomainError as error:
        error_code = error.code
        retry = error.code in {
            "music_timeout",
            "music_request_failed",
            "material_deadline",
            "admission_unavailable",
        }
    with Session(database_engine) as session, session.begin():
        job = _locked(session, job_id)
        if job.claim != claim or job.generation != generation:
            return _public(job)
        try:
            draft = _authority(session, scope, account, lock=True)
            if error_code == "music_request_superseded":
                job.claim = None
                if draft.active_image_generation != job.active_generation:
                    job.status, job.repair_after = "CANCELLED", None
                elif not _skip_obsolete_subscribers(session, job):
                    from .carousel_music_tasks import queue_music

                    job.status = "PENDING"
                    job.generation += 1
                    queue_music(session, job)
                return _public(job)
            job.error_code = error_code
            if retry and job.attempts < 3:
                from .carousel_music_tasks import queue_music

                job.status, job.claim = "PENDING", None
                job.generation += 1
                queue_music(session, job, delay=max(30 * job.attempts, retry_after))
            else:
                if selected:
                    snapshot = asdict(selected)
                    snapshot.pop("preview_url")
                    snapshot["selection_source"] = params["mode"]
                    job.result = {
                        "status": "READY",
                        "music_id": selected.music_id,
                        "snapshot": snapshot,
                    }
                else:
                    job.result = {
                        "status": "NEEDS_SELECTION"
                        if error_code == "music_selection_required"
                        else "FAILED"
                    }
                _publish(session, job, scope)
        except DomainError as error:
            _fail_music(session, job, error.code)
        return _public(job)


def browse_music(
    *,
    database_engine,
    redis_client,
    context,
    draft_id,
    keyword=None,
    cursor=None,
    music_ids=None,
):
    """公共读入口不接受账户/路由；返回试听能力前再次核验当前授权。"""
    from .image_composition_service import resolve_image_scope

    deadline = datetime.now(UTC) + timedelta(seconds=10)
    with Session(database_engine) as session:
        scope = resolve_image_scope(session, context=context, draft_id=draft_id)
        _authority(session, scope, write=False)
        account = _seed(session, scope)
        if not account:
            raise DomainError("image_source_unavailable", "没有可用的已记录种子账户")
    with open_tiktok_gateway(
        database_engine=database_engine,
        redis_client=redis_client,
        context=context,
        route=scope.route,
        task_deadline=deadline,
    ) as gateway:
        kwargs = {
            "advertiser_id": account,
            "budget": _budget(deadline, scope.route, "music.get"),
        }
        if music_ids is not None:
            page = gateway.music.read(music_ids=tuple(music_ids), **kwargs)
        else:
            if not isinstance(keyword, str):
                raise DomainError("music_request_invalid", "搜索关键词无效")
            page = gateway.music.search(keyword=keyword, cursor=cursor, **kwargs)
    with Session(database_engine) as session:
        _authority(session, scope, account, write=False)
        if _seed(session, scope) != account:
            raise DomainError("image_source_unavailable", "种子账户已变化，请刷新")
    return {
        "items": [asdict(i) for i in page.items],
        "next_cursor": page.next_cursor,
        "truncated": page.truncated,
    }
