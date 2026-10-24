"""搭建各阶段耗时剖面（诊断用）：按真实调度节奏记录每类步骤全部完成的时刻。

只在 ACCEPTANCE_SCALE=speed 时运行，不进入发布门禁：
  ACCEPTANCE_SCALE=speed TIMELINE_OUT=/tmp/timeline.json \
    uv run pytest tests/acceptance/test_build_timeline.py -s
"""

import json
import os
import time

import pytest
from sqlalchemy import func
from sqlmodel import Session, select

from app.modules.builds.execution_models import ExecutionStep
from app.modules.materials.cover_models import MaterialCoverJob
from tests.acceptance.scenario import SCALE


@pytest.mark.skipif(SCALE.name != "speed", reason="仅耗时剖面时运行")
@pytest.mark.parametrize("acceptance_scenario", ["wangyan"], indirect=True)
def test_build_stage_timeline(acceptance_scenario):
    scenario = acceptance_scenario
    scenario.prepare()
    scenario.freeze()
    scenario.submit()
    start = time.monotonic()
    marks: dict[str, float] = {}
    samples = []

    def sample() -> bool:
        with Session(scenario.database_engine) as session:
            rows = session.exec(
                select(ExecutionStep.kind, ExecutionStep.status, func.count())
                .where(ExecutionStep.submission_id == scenario.submission_id)
                .group_by(ExecutionStep.kind, ExecutionStep.status)
            ).all()
            covers = session.exec(
                select(MaterialCoverJob.status, func.count())
                .where(MaterialCoverJob.tenant_id == scenario.scope.context.tenant_id)
                .group_by(MaterialCoverJob.status)
            ).all()
        elapsed = round(time.monotonic() - start, 1)
        state = {}
        for kind, status, count in rows:
            state.setdefault(kind, {})[status] = count
        cover_state = dict(covers)
        for kind, statuses in state.items():
            if set(statuses) == {"SUCCEEDED"} and kind not in marks:
                marks[kind] = elapsed
        if cover_state and set(cover_state) == {"READY"} and "COVERS" not in marks:
            marks["COVERS"] = elapsed
        samples.append({"t": elapsed, "steps": state, "covers": cover_state})
        return scenario.view().status not in {"QUEUED", "RUNNING"}

    scenario.runtime.drive_until(
        sample, timeout=float(os.environ.get("TIMELINE_TIMEOUT", "1800"))
    )
    result = {
        "total": round(time.monotonic() - start, 1),
        "marks": marks,
        "status": scenario.view().status,
    }
    out = os.environ.get("TIMELINE_OUT")
    if out:
        with open(out, "w") as handle:
            json.dump(
                {"result": result, "samples": samples}, handle, ensure_ascii=False
            )
    print(json.dumps(result, ensure_ascii=False))  # noqa: T201 诊断输出
