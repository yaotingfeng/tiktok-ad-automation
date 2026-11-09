import csv
import io
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from app.core.errors import DomainError
from app.modules.operation_logs.parser import parse_export, utc_window

HEADER = [
    "Time",
    "log_object_type",
    "Object ID",
    "Object",
    "Operator",
    "Source",
    "App ID",
    "Activity details",
]


def export(*details, records=None):
    entries = (
        records
        if records is not None
        else [
            ("2026-11-01 08:02", "Campaign", "campaign-test", detail)
            for detail in details
        ]
    )
    stream = io.StringIO()
    writer = csv.writer(stream)
    for category, predicate in [
        ("Budget changes", lambda d: "budget" in d.get("name", "").lower()),
        ("Bid changes", lambda d: d.get("name", "").lower() == "minimum roas"),
        (
            "Status changes",
            lambda d: (
                d.get("action", "").strip() == "Change"
                and d.get("name", "").lower()
                in {"campaign", "ad group", "review status"}
            ),
        ),
    ]:
        writer.writerow(
            [
                category,
                str(sum(any(predicate(d) for d in entry[3]) for entry in entries)),
            ]
        )
    writer.writerow([])
    writer.writerow(HEADER)
    for stamp, kind, identity, detail in entries:
        writer.writerow(
            [
                stamp,
                kind,
                identity,
                "Settings",
                "private",
                "Marketing API",
                "app-test",
                json.dumps(detail, indent=2),
            ]
        )
    return "\ufeff" + stream.getvalue()


def test_csv_multiline_bom_and_multiplicity():
    change = [
        {
            "action": "Change ",
            "name": "minimum ROAS",
            "before_after": [{"before": "1.2", "after": "1.1"}],
        }
    ]
    rows = parse_export(export(change, change))
    assert len(rows) == 2
    assert rows[0].occurred_at == datetime(2026, 11, 1, 8, 2, tzinfo=UTC)
    assert rows[0].identity != rows[1].identity
    assert rows[0].attribution == "UNKNOWN"  # API不代表自动操作
    assert rows == parse_export(export(change, change))


def test_complete_empty_and_unknown_are_different():
    assert parse_export(export()) == ()
    for invalid in (
        "",
        "error",
        "Time,Object ID\n",
        export().replace("Bid changes,0", "Bid changes,2"),
    ):
        with pytest.raises(DomainError):
            parse_export(invalid)


def test_archived_download_wrapper():
    raw = export()
    assert (
        parse_export(
            json.dumps({"file_name": "log.csv", "file_data": "b'" + raw + "'"})
        )
        == ()
    )


def test_dst_window_uses_real_elapsed_time():
    start, end = utc_window(date(2026, 11, 1), date(2026, 11, 1), "America/New_York")
    assert end - start == timedelta(hours=25)
    with pytest.raises(DomainError):
        utc_window(date(2026, 10, 15), date(2026, 11, 13), "America/New_York")
