# Real PostgreSQL workflow rows; only the provider's HTTP transport is fake.
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import delete
from sqlmodel import Session, select

from app.core.config import settings
from app.core.credentials import encrypt_credentials
from app.core.db import engine
from app.core.errors import DomainError
from app.models import User
from app.modules.providers.link_steps import (
    check_jiashu_config,
    run_link_item,
    should_send,
)
from app.modules.providers.models import (
    LinkPreparation,
    LinkPreparationItem,
    PromotionLink,
    ProviderApplication,
    ProviderConnection,
    ProviderDrama,
    ProviderEffect,
    ProviderRemoteScope,
)
from app.modules.tenants.models import AuditEvent, Tenant, TenantMembership
from tests.modules.conftest import create_context


def test_existing_episode_cannot_be_silently_overwritten():
    with pytest.raises(DomainError) as error:
        check_jiashu_config(
            {"drama_num": 1, "jump_url": "https://example.test/a"}, {"episode": 2}
        )
    assert error.value.code == "config_conflict"


@pytest.mark.parametrize("status", ["sending", "result_unknown", "succeeded", "failed"])
def test_unknown_or_finished_write_is_never_replayed(status):
    assert should_send(status) is False


@pytest.mark.parametrize("status", ["pending", "confirmed_absent"])
def test_only_unsent_or_proven_absent_effect_can_send(status):
    assert should_send(status) is True


@pytest.mark.parametrize(
    "existing,requested",
    [
        ({"jump_url": "https://example.test/a"}, {"episode": 1}),
        ({"jump_url": "https://example.test/a", "drama_num": True}, {"episode": 1}),
        (
            {"jump_url": "https://example.test/a", "drama_num": 1},
            {"episode": 1, "charge_level": 2},
        ),
    ],
)
def test_unverifiable_configuration_does_not_silently_match(existing, requested):
    with pytest.raises(DomainError) as error:
        check_jiashu_config(existing, requested)
    assert error.value.code == "config_unverifiable"


class SimulatedCrash(BaseException):
    pass


class RemoteFixture:
    def __init__(self):
        self.calls = []
        self.channel_exists = False
        self.saved = {}
        self.fail = None
        self.crash_creates_channel = True
        self.drama_rows = [{"video_id": "101", "name": "Moon", "language": "en"}]
        self.page_count = None

    def __call__(self, request):
        import json

        payload = json.loads(request.content)
        path = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(path)
        if path == "getVideoList":
            data = {"data": self.drama_rows, "count": len(self.drama_rows)}
        elif path == "getChannelList":
            rows = (
                [{"channel": "test_101", "remark": "Moon"}]
                if self.channel_exists
                else []
            )
            data = {"data": rows, "count": len(rows)}
            if self.page_count is not None:
                data["count"] = self.page_count
        elif path == "create":
            self.channel_exists = self.crash_creates_channel
            if self.fail == "create_crash":
                self.fail = None
                raise SimulatedCrash()
            self.channel_exists = True
            data = True
        elif path == "generateGuideUrl":
            if self.fail == "generate_unknown":
                self.fail = None
                raise httpx.ReadTimeout("private-provider-response")
            data = {
                "url": "https://www.tiktok.com/t/fixture?channel=test_101&vid=101&dramaNum=1&charge_level=0",
                "minis_path": "fixture-path",
            }
        elif path == "saveGuideUrl":
            self.saved = {
                "vid": payload["vid"],
                "drama_num": payload["drama_num"],
                "jump_url": payload["jump_url"],
                "minis_path": payload["minis_path"],
            }
            if self.fail == "save_unknown":
                self.fail = None
                raise httpx.ReadTimeout("private-provider-response")
            data = True
        elif path == "getGuideUrl":
            if self.fail == "read_crash":
                self.fail = None
                raise SimulatedCrash()
            data = {"config": self.saved}
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"code": "0000", "data": data})


@pytest.fixture
def workflow(monkeypatch):
    monkeypatch.setattr(
        settings, "CONNECTION_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    with Session(engine) as session:
        context = create_context(session)
        verification = uuid4()
        connection = ProviderConnection(
            tenant_id=context.tenant_id,
            kind="jiashu",
            display_name="Offline fixture",
            status="active",
            credential_version=1,
            verification_token=verification,
            encrypted_credentials=encrypt_credentials(
                tenant_id=context.tenant_id, value={"session": "fake-private-session"}
            ),
        )
        session.add(connection)
        session.flush()
        app = ProviderApplication(
            tenant_id=context.tenant_id,
            connection_id=connection.id,
            external_id="external-app",
            name="Fixture application",
            channel_config={
                "channel_prefix": "test_",
                "verification_token": str(verification),
            },
        )
        session.add(app)
        session.flush()
        connection_id = connection.id
        session.commit()
    remote = RemoteFixture()
    yield context, connection_id, remote
    with Session(engine) as session:
        for model in (
            ProviderEffect,
            ProviderRemoteScope,
            PromotionLink,
            LinkPreparationItem,
            LinkPreparation,
            ProviderDrama,
            ProviderApplication,
            ProviderConnection,
            AuditEvent,
            TenantMembership,
        ):
            session.exec(delete(model).where(model.tenant_id == context.tenant_id))
        session.exec(delete(Tenant).where(Tenant.id == context.tenant_id))
        session.exec(delete(User).where(User.id == context.actor_id))
        session.commit()


def add_item(workflow, *, config=None, raw="Moon"):
    context, connection_id, _ = workflow
    with Session(engine) as session:
        preparation = LinkPreparation(
            tenant_id=context.tenant_id,
            actor_id=context.actor_id,
            request_id=uuid4(),
            request_digest="a" * 64,
            connection_id=connection_id,
            application_id="external-app",
            config={"episode": 1} if config is None else config,
        )
        session.add(preparation)
        session.flush()
        item = LinkPreparationItem(
            tenant_id=context.tenant_id,
            preparation_id=preparation.id,
            line_no=1,
            raw_input=raw,
        )
        session.add(item)
        session.flush()
        item_id = item.id
        session.commit()
        return item_id


def unit(workflow, item_id):
    context, _, remote = workflow
    before = len(remote.calls)
    with Session(engine) as session:
        run_link_item(
            session,
            context=context,
            item_id=item_id,
            transport=httpx.MockTransport(remote),
        )
    assert len(remote.calls) - before <= 1
    with Session(engine) as session:
        item = session.get(LinkPreparationItem, item_id)
        return item.status, item.resolved


def drive(workflow, item_id, *, stop="ready", max_units=15):
    for _ in range(max_units):
        status, result = unit(workflow, item_id)
        if status == stop:
            return result
    pytest.fail(
        f"Did not reach {stop}; last status={status}, code={result.get('error_code')}"
    )


def expire_item(item_id):
    with Session(engine) as session:
        item = session.get(LinkPreparationItem, item_id)
        work = dict(item.resolved["_work"])
        work["claim_until"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        item.resolved = {**item.resolved, "_work": work}
        session.add(item)
        session.commit()


def test_entire_jiashu_chain_commits_four_independent_effects(workflow):
    item_id = add_item(workflow)
    result = drive(workflow, item_id)
    assert result["external_drama_id"] == "101" and result["protected_base"] == ""
    assert "private" not in repr(result)
    assert workflow[2].calls == [
        "getVideoList",
        "getChannelList",
        "create",
        "getGuideUrl",
        "generateGuideUrl",
        "saveGuideUrl",
        "getGuideUrl",
    ]
    with Session(engine) as session:
        effects = session.exec(
            select(ProviderEffect).where(
                ProviderEffect.tenant_id == workflow[0].tenant_id
            )
        ).all()
        assert {effect.step for effect in effects} == {
            "create",
            "generate",
            "save",
            "read",
        }
        assert all(effect.status == "succeeded" for effect in effects)
        scope = session.exec(
            select(ProviderRemoteScope).where(
                ProviderRemoteScope.tenant_id == workflow[0].tenant_id
            )
        ).one()
        assert scope.status == "idle" and scope.active_item_id is None


def test_ten_consumers_reuse_one_drama_link(workflow):
    first = drive(workflow, add_item(workflow))
    for _ in range(9):
        assert drive(workflow, add_item(workflow))["link_id"] == first["link_id"]
    assert workflow[2].calls.count("create") == 1
    assert workflow[2].calls.count("generateGuideUrl") == 1
    assert workflow[2].calls.count("saveGuideUrl") == 1


def test_crash_after_channel_creation_recovers_without_duplicate_create(workflow):
    item_id = add_item(workflow)
    unit(workflow, item_id)
    unit(workflow, item_id)
    workflow[2].fail = "create_crash"
    with pytest.raises(SimulatedCrash):
        unit(workflow, item_id)
    before = len(workflow[2].calls)
    unit(workflow, item_id)
    assert len(workflow[2].calls) == before  # live claim forbids early recovery
    expire_item(item_id)
    drive(workflow, item_id)
    assert workflow[2].calls.count("create") == 1


def test_save_timeout_reads_saved_result_without_second_write(workflow):
    item_id = add_item(workflow)
    workflow[2].fail = "save_unknown"
    drive(workflow, item_id, stop="result_unknown")
    drive(workflow, item_id)
    assert workflow[2].calls.count("create") == 1
    assert workflow[2].calls.count("saveGuideUrl") == 1


def test_unknown_generation_does_not_replay_and_keeps_cross_config_scope(workflow):
    first = add_item(workflow)
    workflow[2].fail = "generate_unknown"
    drive(workflow, first, stop="result_unknown")
    for _ in range(3):
        assert unit(workflow, first)[0] == "result_unknown"
    second = add_item(workflow, config={"episode": 2})
    unit(workflow, second)
    status, result = unit(workflow, second)
    assert status == "pending" and result["error_code"] == "provider_scope_busy"
    assert workflow[2].calls.count("generateGuideUrl") == 1
    assert workflow[2].calls.count("saveGuideUrl") == 0
    with Session(engine) as session:
        scope = session.exec(
            select(ProviderRemoteScope).where(
                ProviderRemoteScope.tenant_id == workflow[0].tenant_id
            )
        ).one()
        assert scope.status == "result_unknown" and scope.active_item_id == first


def test_complete_absence_proof_is_required_before_retrying_crashed_create(workflow):
    item_id = add_item(workflow)
    unit(workflow, item_id)
    unit(workflow, item_id)
    workflow[2].fail, workflow[2].crash_creates_channel = "create_crash", False
    with pytest.raises(SimulatedCrash):
        unit(workflow, item_id)
    expire_item(item_id)
    drive(workflow, item_id)
    assert workflow[2].calls.count("create") == 2


def test_crashed_read_is_retried_and_does_not_hold_an_unknown_write(workflow):
    item_id = add_item(workflow)
    for _ in range(3):
        unit(workflow, item_id)
    workflow[2].fail = "read_crash"
    with pytest.raises(SimulatedCrash):
        unit(workflow, item_id)
    expire_item(item_id)
    drive(workflow, item_id)
    assert workflow[2].calls.count("create") == 1
    with Session(engine) as session:
        scope = session.exec(
            select(ProviderRemoteScope).where(
                ProviderRemoteScope.tenant_id == workflow[0].tenant_id
            )
        ).one()
        assert scope.status == "idle"


def test_title_ambiguity_never_guesses_or_creates(workflow):
    workflow[2].drama_rows.append({"video_id": "102", "name": "Moon", "language": "es"})
    result = drive(workflow, add_item(workflow), stop="needs_resolution")
    assert len(result["candidates"]) == 2 and result["drama_id"] is None
    assert workflow[2].calls == ["getVideoList"]


def test_existing_incompatible_episode_does_not_overwrite(workflow):
    workflow[2].channel_exists = True
    workflow[2].saved = {
        "vid": "101",
        "drama_num": 2,
        "jump_url": "https://www.tiktok.com/t/fixture?channel=test_101&vid=101&dramaNum=2&charge_level=0",
    }
    item_id = add_item(workflow)
    result = drive(workflow, item_id, stop="config_conflict")
    assert result["error_code"] == "config_conflict"
    assert "create" not in workflow[2].calls and "saveGuideUrl" not in workflow[2].calls
    from app.modules.providers.service import get_link_results

    with Session(engine) as session:
        item = session.get(LinkPreparationItem, item_id)
        public = get_link_results(
            session, context=workflow[0], task_id=item.preparation_id
        ).items[0]
        assert public.existing_config == {"episode": 2}
        assert public.requested_config == {"episode": 1}


def test_incomplete_history_cannot_authorize_create(workflow):
    workflow[2].page_count = 100  # inconsistent short page cannot prove exhaustion
    result = drive(workflow, add_item(workflow), stop="failed")
    assert result["error_code"] in {"lookup_incomplete", "provider_schema_unsupported"}
    assert "create" not in workflow[2].calls


@pytest.mark.parametrize(
    "bad_work",
    [
        [],
        {"claim_token": "not-a-uuid", "claim_until": "2030-01-01T00:00:00+00:00"},
        {"claim_token": str(uuid4()), "claim_until": "2030-01-01T00:00:00"},
        {"claim_token": str(uuid4()), "claim_until": "2030-01-01T00:00:00+08:00"},
        {"stage": "create", "drama": {}},
    ],
)
def test_corrupt_checkpoint_is_quarantined_without_network(workflow, bad_work):
    item_id = add_item(workflow)
    with Session(engine) as session:
        item = session.get(LinkPreparationItem, item_id)
        item.resolved = {"_work": bad_work}
        session.add(item)
        session.commit()
    status, result = unit(workflow, item_id)
    assert status == "result_unknown"
    assert result["error_code"] == "provider_state_invalid"
    assert result["_work"]["_invalid_work"] == bad_work
    assert unit(workflow, item_id)[0] == "result_unknown"
    assert workflow[2].calls == []


def test_revoked_actor_is_blocked_before_provider_request(workflow):
    item_id = add_item(workflow)
    with Session(engine) as session:
        membership = session.get(
            TenantMembership, (workflow[0].tenant_id, workflow[0].actor_id)
        )
        membership.active = False
        session.add(membership)
        session.commit()
    assert unit(workflow, item_id)[0] == "blocked_auth"
    assert workflow[2].calls == []


def test_network_runs_after_claim_commit_without_database_row_locks(workflow):
    item_id = add_item(workflow)
    remote = workflow[2]

    def inspect_transport(request):
        # NOWAIT would fail immediately if orchestration kept either row locked.
        with Session(engine) as observer:
            observed = observer.exec(
                select(LinkPreparationItem)
                .where(LinkPreparationItem.id == item_id)
                .with_for_update(nowait=True)
            ).one()
            assert observed.resolved["_work"]["claim_token"]
            effects = observer.exec(
                select(ProviderEffect)
                .where(ProviderEffect.tenant_id == workflow[0].tenant_id)
                .with_for_update(nowait=True)
            ).all()
            if request.url.path.endswith("/create"):
                assert any(
                    effect.step == "create" and effect.status == "sending"
                    for effect in effects
                )
            observer.rollback()
        return remote(request)

    for _ in range(7):
        with Session(engine) as session:
            run_link_item(
                session,
                context=workflow[0],
                item_id=item_id,
                transport=httpx.MockTransport(inspect_transport),
            )
    with Session(engine) as session:
        assert session.get(LinkPreparationItem, item_id).status == "ready"


def test_late_write_response_cannot_cross_item_or_effect_attempt_fence(workflow):
    item_id = add_item(workflow)
    unit(workflow, item_id)
    unit(workflow, item_id)
    winner_token = uuid4()
    remote = workflow[2]

    def fenced_response(request):
        response = remote(request)
        with Session(engine) as other_worker:
            item = other_worker.get(LinkPreparationItem, item_id)
            work = dict(item.resolved["_work"])
            work["claim_token"] = str(winner_token)
            item.resolved = {**item.resolved, "_work": work}
            other_worker.add(item)
            effect = other_worker.exec(
                select(ProviderEffect).where(
                    ProviderEffect.tenant_id == workflow[0].tenant_id,
                    ProviderEffect.step == "create",
                )
            ).one()
            effect.attempt_token = winner_token
            other_worker.add(effect)
            other_worker.commit()
        return response

    with Session(engine) as session:
        run_link_item(
            session,
            context=workflow[0],
            item_id=item_id,
            transport=httpx.MockTransport(fenced_response),
        )
    with Session(engine) as session:
        item = session.get(LinkPreparationItem, item_id)
        effect = session.exec(
            select(ProviderEffect).where(
                ProviderEffect.tenant_id == workflow[0].tenant_id,
                ProviderEffect.step == "create",
            )
        ).one()
        assert item.resolved["_work"]["claim_token"] == str(winner_token)
        assert effect.status == "sending" and effect.attempt_token == winner_token
        assert item.status != "ready"


def test_recovery_cannot_overwrite_a_different_effect_attempt(workflow):
    item_id = add_item(workflow)
    workflow[2].fail = "save_unknown"
    drive(workflow, item_id, stop="result_unknown")
    new_token = uuid4()
    with Session(engine) as session:
        effect = session.exec(
            select(ProviderEffect).where(
                ProviderEffect.tenant_id == workflow[0].tenant_id,
                ProviderEffect.step == "save",
            )
        ).one()
        effect.attempt_token = new_token
        session.add(effect)
        session.commit()
    status, result = unit(workflow, item_id)
    assert (
        status == "result_unknown" and result["error_code"] == "provider_state_invalid"
    )
    with Session(engine) as session:
        effect = session.exec(
            select(ProviderEffect).where(
                ProviderEffect.tenant_id == workflow[0].tenant_id,
                ProviderEffect.step == "save",
            )
        ).one()
        assert effect.status == "result_unknown" and effect.attempt_token == new_token
        assert not session.exec(
            select(PromotionLink).where(
                PromotionLink.tenant_id == workflow[0].tenant_id,
            )
        ).all()


def test_revocation_after_write_retains_acknowledgement_and_stops_next_step(workflow):
    item_id = add_item(workflow)
    unit(workflow, item_id)
    unit(workflow, item_id)

    def revoked_after_response(request):
        response = workflow[2](request)
        with Session(engine) as session:
            membership = session.get(
                TenantMembership, (workflow[0].tenant_id, workflow[0].actor_id)
            )
            membership.active = False
            session.add(membership)
            session.commit()
        return response

    with Session(engine) as session:
        run_link_item(
            session,
            context=workflow[0],
            item_id=item_id,
            transport=httpx.MockTransport(revoked_after_response),
        )
    with Session(engine) as session:
        assert session.get(LinkPreparationItem, item_id).status == "blocked_auth"
        effect = session.exec(
            select(ProviderEffect).where(
                ProviderEffect.tenant_id == workflow[0].tenant_id,
                ProviderEffect.step == "create",
            )
        ).one()
        assert effect.status == "succeeded"
    assert workflow[2].calls == ["getVideoList", "getChannelList", "create"]


def test_crashed_worker_checkpoint_corruption_does_not_release_remote_scope(workflow):
    item_id = add_item(workflow)
    unit(workflow, item_id)
    unit(workflow, item_id)
    workflow[2].fail = "create_crash"
    with pytest.raises(SimulatedCrash):
        unit(workflow, item_id)
    with Session(engine) as session:
        item = session.get(LinkPreparationItem, item_id)
        item.resolved = {"_work": {"claim_until": "invalid"}}
        session.add(item)
        session.commit()
    status, result = unit(workflow, item_id)
    assert (
        status == "result_unknown" and result["error_code"] == "provider_state_invalid"
    )
    with Session(engine) as session:
        scope = session.exec(
            select(ProviderRemoteScope).where(
                ProviderRemoteScope.tenant_id == workflow[0].tenant_id,
            )
        ).one()
        assert scope.status == "result_unknown" and scope.active_item_id == item_id
    assert workflow[2].calls.count("create") == 1
