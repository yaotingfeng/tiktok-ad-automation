"""The comparison remains reproducible after the production migration ships."""

from contextlib import contextmanager

from scripts import benchmark_dispatch_index as benchmark


def test_probe_baseline_removes_only_its_owned_database_fix(monkeypatch):
    original = benchmark.owned_database

    @contextmanager
    def with_shipped_index(url):
        with original(url) as engine:
            with engine.begin() as connection:
                connection.exec_driver_sql("""
                    CREATE INDEX IF NOT EXISTS ix_dispatch_expansion_pending
                    ON pending_dispatch(tenant_id, available_at, id)
                    WHERE published_at IS NULL
                      AND task_name = 'builds.expand_submission'
                """)
            yield engine

    monkeypatch.setattr(benchmark, "owned_database", with_shipped_index)
    result = benchmark.measure(20000)
    assert result["complete"]

    def indexes(node):
        return [node.get("Index Name")] + [
            name for child in node.get("Plans", []) for name in indexes(child)
        ]

    assert "ix_dispatch_expansion_pending" not in indexes(
        result["before"]["plan"]["Plan"]
    )
    assert "ix_dispatch_expansion_pending_probe" in indexes(
        result["after_generic_literal_task"]["plan"]["Plan"]
    )
