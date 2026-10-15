from uuid import uuid4

import pytest

from app.integrations.tiktok.contracts.ads import EntityRef
from app.integrations.tiktok.contracts.management import (
    ManagementCommand,
    operation_for_field,
)


def test_rename_command_campaign_only_and_single_key():
    campaign = EntityRef(uuid4(), "adv", "campaign", "c1")
    command = ManagementCommand(campaign, "name", {}, {"campaign_name": "新名"})
    assert command.operation == "rename"
    assert operation_for_field("name") == "rename"
    with pytest.raises(ValueError):
        ManagementCommand(
            EntityRef(uuid4(), "adv", "adgroup", "g1"),
            "name",
            {},
            {"campaign_name": "x"},
        )
    with pytest.raises(ValueError):
        ManagementCommand(campaign, "name", {}, {"campaign_name": "x", "budget": "1"})
