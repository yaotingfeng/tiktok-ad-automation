"""Backup tooling must match the server without exposing connection details."""

from subprocess import CompletedProcess

import pytest

from tests.pg_backup import backup_tools, run_backup_tool


@pytest.mark.parametrize(
    "server,dump,restore,expected",
    [
        (18, 17, 18, "pg_dump major 17 is older than server major 18"),
        (18, 18, 17, "pg_restore major 17 is older than pg_dump major 18"),
        (18, 18, 18, None),
        (17, 18, 18, None),
    ],
)
def test_backup_major_compatibility(
    monkeypatch, tmp_path, server, dump, restore, expected
):
    monkeypatch.setenv("PG_BIN_DIR", str(tmp_path))
    for name in ("pg_dump", "pg_restore"):
        (tmp_path / name).touch(mode=0o700)
    calls = []

    def run(argv, **_kwargs):
        calls.append(argv)
        major = dump if argv[0].endswith("pg_dump") else restore
        return CompletedProcess(argv, 0, f"pg_dump (PostgreSQL) {major}.4", "")

    monkeypatch.setattr("tests.pg_backup.subprocess.run", run)
    if expected:
        with pytest.raises(RuntimeError, match=expected):
            backup_tools(server)
    else:
        assert backup_tools(server) == (
            str(tmp_path / "pg_dump"),
            str(tmp_path / "pg_restore"),
        )
    assert all(str(tmp_path) in call[0] and call[1:] == ["--version"] for call in calls)


def test_explicit_binary_directory_never_silently_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("PG_BIN_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="pg_dump unavailable in PG_BIN_DIR"):
        backup_tools(18)


def test_failed_backup_diagnostics_exclude_credentials_and_stderr(monkeypatch):
    secret = "synthetic-password-never-log"
    monkeypatch.setattr(
        "tests.pg_backup.subprocess.run",
        lambda *args, **kwargs: CompletedProcess(
            args, 1, "", f"postgresql://user:{secret}@host/db"
        ),
    )
    with pytest.raises(RuntimeError) as caught:
        run_backup_tool(
            "/trusted/pg_dump", ["--file", "/private/output"], {"PGPASSWORD": secret}
        )
    assert (
        str(caught.value)
        == "pg_dump failed (exit 1); backup/restore validation was not completed"
    )
    assert secret not in str(caught.value)
