from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlmodel import select

from app.modules.strategies.models import Strategy, StrategyVersion
from app.modules.strategies.naming import render_names
from app.modules.strategies.saved_config import read_saved_config
from app.modules.strategies.schemas import StrategyConfig
from app.modules.strategies.service import (
    get_strategy,
    get_version_record,
    list_versions,
)
from tests.modules.strategies.test_naming import ARGS
from tests.modules.strategies.test_versions import config


@pytest.mark.parametrize(
    "template,expected",
    [
        (None, "{provider_drama}-{drama_id}"),
        (
            "{provider_pinyin}-{drama_name}-{drama_id}-{random}",
            "{provider_drama}-{drama_id}",
        ),
        (
            "{provider_pinyin}-{drama_name}-{drama_id}-{YYYYMMDD}-{random}",
            "{provider_drama}-{drama_id}-{YYYYMMDD}",
        ),
        ("{drama_id}-{random}", "{provider_drama}-{drama_id}"),
        (
            "{YYYYMMDD}-{{literal}}-{drama_id}-{provider_pinyin}-{drama_name}-{random}",
            "{provider_drama}-{YYYYMMDD}-{{literal}}-{drama_id}",
        ),
    ],
)
def test_saved_format_conversion_preserves_custom_content_without_mutation(
    template, expected
):
    saved = config().model_dump(mode="json")
    saved["campaign_suffix"] = "-旧后缀-{batch_short_id}"
    if template is None:
        saved.pop("campaign_name_template")
    else:
        saved["campaign_name_template"] = template
    original = deepcopy(saved)
    current = read_saved_config(saved)
    assert current.campaign_name_template == expected
    assert "campaign_suffix" not in current.model_dump()
    assert saved == original
    # 转换结果进入唯一的新引擎，网眼也包含独立剧目 ID。
    assert "106001" in render_names(**ARGS, template=current.campaign_name_template)[0]


def test_new_requests_reject_removed_suffix():
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(
            config().model_dump() | {"campaign_suffix": "-{batch_short_id}"}
        )


def test_reading_historical_versions_preserves_database_record_and_digest(
    session, context
):
    saved = config().model_dump(mode="json") | {
        "campaign_suffix": "-{YYYYMMDD}-{batch_short_id}",
        "campaign_name_template": "{provider_pinyin}-{drama_name}-{drama_id}-{random}",
    }
    strategy = Strategy(tenant_id=context.tenant_id, name="历史命名", latest_version=1)
    session.add(strategy)
    session.flush()
    version = StrategyVersion(
        tenant_id=context.tenant_id,
        strategy_id=strategy.id,
        number=1,
        copy_pool_version_id=config().copy_pool_version,
        config=saved,
        budget=config().budget,
        target_roas=config().target_roas,
        created_by=context.actor_id,
        request_id=uuid4(),
        request_digest="a" * 64,
        request_kind="create",
    )
    session.add(version)
    session.flush()
    session.refresh(version)
    original = version.model_dump(mode="json")
    records = [
        get_strategy(session, context=context, strategy_id=strategy.id),
        get_version_record(session, context=context, version_id=version.id),
        list_versions(session, context=context, strategy_id=strategy.id).items[0],
    ]
    for record in records:
        assert record.config.campaign_name_template == "{provider_drama}-{drama_id}"
    session.expire_all()
    assert (
        session.exec(select(StrategyVersion).where(StrategyVersion.id == version.id))
        .one()
        .model_dump(mode="json")
        == original
    )
