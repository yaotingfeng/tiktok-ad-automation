"""No-storage-side-effect defaults for the explicit-scope reconciliation CLI."""

import json
from uuid import uuid4

from tests.acceptance.test_r2_probe import load_script


def test_reconcile_cli_requires_explicit_tenant_bc_actor_before_runtime():
    import pytest

    script = load_script("reconcile-r2.py")
    with pytest.raises(SystemExit) as caught:
        script.main([])
    assert caught.value.code == 2


def test_reconcile_cli_default_requests_one_readonly_scoped_page(capsys):
    script = load_script("reconcile-r2.py")
    tenant, actor = uuid4(), uuid4()
    seen = []

    def execute(args):
        seen.append(args)
        return {"examined": 0, "queued": 0, "next_cursor": None}

    result = script.main(
        ["--tenant-id", str(tenant), "--actor-id", str(actor), "--bc-id", "bc-a"],
        executor=execute,
    )
    assert result == 0 and len(seen) == 1
    assert (
        seen[0].mode == "database" and seen[0].enqueue is False and seen[0].limit == 100
    )
    assert (
        seen[0].tenant_id == tenant
        and seen[0].actor_id == actor
        and seen[0].bc_id == "bc-a"
    )
    assert json.loads(capsys.readouterr().out)["queued"] == 0


def test_reconcile_cli_never_prints_runtime_credentials(capsys):
    script = load_script("reconcile-r2.py")

    def execute(_args):
        raise RuntimeError(
            "credential=do-not-print https://signed.invalid/?signature=do-not-print"
        )

    assert (
        script.main(
            [
                "--tenant-id",
                str(uuid4()),
                "--actor-id",
                str(uuid4()),
                "--bc-id",
                "bc-a",
            ],
            executor=execute,
        )
        == 1
    )
    assert "do-not-print" not in capsys.readouterr().out
