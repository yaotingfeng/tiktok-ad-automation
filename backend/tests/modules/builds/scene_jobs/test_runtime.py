from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import DomainError
from app.modules.builds import scene
from app.modules.strategies.copy_pool import seed_copies


@pytest.mark.parametrize("hard", [0, False, 46, "45"])
def test_explicit_invalid_task_override_does_not_fall_back(monkeypatch, hard):
    monkeypatch.setattr(
        scene,
        "current_process",
        lambda: SimpleNamespace(daemon=True, name="ForkPoolWorker-1"),
    )
    monkeypatch.setattr(
        scene,
        "current_task",
        SimpleNamespace(
            time_limit=45,
            request=SimpleNamespace(
                timelimit=(hard, None), called_directly=False, is_eager=False
            ),
        ),
    )
    with pytest.raises(DomainError):
        scene._require_bounded_worker()


@pytest.mark.parametrize("value", [59, 604801])
def test_scene_engineering_age_configuration_rejects_invalid_values(value):
    with pytest.raises(ValidationError):
        Settings(SCENE_MAX_AGE_SECONDS=value)


def test_english_copy_pool_respects_separate_application_policy():
    from app.modules.builds.scene_constraints import constraints_for

    constraints, _ = constraints_for("USD")
    assert constraints["platform_copy_length"] is None
    assert len(seed_copies()) >= 30
    for copy in seed_copies():
        assert (
            copy.text.strip()
            and len(copy.text) <= constraints["copy_policy"]["maximum"]
        )
