"""手动链接通过真实准备/预览/执行链；外部请求仅由离线传输夹具响应。"""

import json
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlmodel import Session, select

from app.modules.builds import drafts
from app.modules.builds.models import DraftPreparation
from app.modules.materials.models import AccountMaterial
from app.modules.providers.models import LinkPreparation
from app.modules.strategies.models import StrategyVersion
from app.modules.strategies.schemas import StrategyConfig
from app.modules.strategies.service import append_version
from tests.acceptance.scenario import DRAMAS


@pytest.mark.parametrize("acceptance_scenario", [{"material_count": 1}], indirect=True)
def test_other_provider_manual_url_reaches_official_sdk_without_provider_calls(
    acceptance_scenario,
):
    case = acceptance_scenario
    url = "https://www.tiktok.com/minis/acceptance-minis?channel=manual%2Bsource&campaign=a%26b"
    with Session(case.database_engine) as session, session.begin():
        # 本用例使用只有原文件的素材输入，验证既有原件上传链；共享另有专门合同测试。
        session.exec(
            delete(AccountMaterial).where(
                AccountMaterial.tenant_id == case.scope.context.tenant_id
            )
        )
        version = session.get(StrategyVersion, case.scope.version_id)
        version_id = append_version(
            session,
            context=case.scope.context,
            strategy_id=version.strategy_id,
            config=StrategyConfig.model_validate(version.config).model_copy(
                update={"group_size": 1, "creative_count": 1}
            ),
        )
        case.draft_id = drafts.create_draft(
            session,
            context=case.scope.context,
            bc_id=case.scope.bc_id,
            strategy_version_id=version_id,
            custom_provider_name="合成版权方",
            provider_connection_id=None,
            application_id=None,
            drama_lines=[DRAMAS[0]],
            account_lines=[case.scope.accounts[0]],
            link_config={},
            manual_links=[{"line_no": 1, "url": url}],
        )
        preparation_id = drafts.prepare_draft(
            session,
            context=case.scope.context,
            draft_id=case.draft_id,
            request_id=uuid4(),
        )

    def prepared():
        with Session(case.database_engine) as session:
            return session.get(DraftPreparation, preparation_id).status != "PENDING"

    case.runtime.drive_until(prepared)
    case.freeze()
    with Session(case.database_engine) as session:
        assert (
            session.exec(
                select(LinkPreparation).where(
                    LinkPreparation.tenant_id == case.scope.context.tenant_id
                )
            ).all()
            == []
        )
    assert not case.runtime.wire.smart.calls
    case.submit()
    case.runtime.drive_until(lambda: case.view().status not in {"QUEUED", "RUNNING"})
    assert case.view().succeeded.model_dump() == {
        "campaign_count": 1,
        "adgroup_count": 1,
        "ad_count": 1,
    }, case.runtime.diagnostics()
    payloads = [
        call for call in case.runtime.wire.smart.calls if call["method"] == "POST"
    ]
    assert len(payloads) == 3
    assert any(url in json.dumps(call, ensure_ascii=False) for call in payloads)
