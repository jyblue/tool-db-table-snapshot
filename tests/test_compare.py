import datetime as dt

import pytest

from snapshot import compare


class FakeConnection:
    def __init__(self):
        self.calls = []


def fake_rows(conn, sql, args=()):
    conn.calls.append((sql, args))
    if "ENGINE" in sql:
        return [("InnoDB",)]
    if "information_schema.TABLES" in sql:
        return [("orders",), ("_snapshot_runs",)]
    if "information_schema.COLUMNS" in sql:
        return [
            ("snapshot_date", "date", "NO", None, None, ""),
            ("id", "int", "NO", None, None, ""),
            ("status", "varchar(10)", "NO", "utf8mb4", "utf8mb4_bin", ""),
        ]
    if "information_schema.STATISTICS" in sql:
        return [("PRIMARY", 0, 1, "snapshot_date", None), ("PRIMARY", 0, 2, "id", None)]
    if "DISTINCT" in sql:
        return [(dt.date(2026, 9, 7),), (dt.date(2026, 9, 6),)]
    if args[0] == dt.date(2026, 9, 6):
        return [(dt.date(2026, 9, 6), 1, "old"), (dt.date(2026, 9, 6), 2, "same")]
    return [(dt.date(2026, 9, 7), 1, "new"), (dt.date(2026, 9, 7), 3, "added")]


def test_compare_reports_added_removed_changed(monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(compare.db, "rows", fake_rows)
    result = compare.compare(conn, "target", "orders", dt.date(2026, 9, 6), dt.date(2026, 9, 7))
    assert result["older_count"] == 2
    assert result["newer_count"] == 2
    assert result["added"][0]["key"] == (3,)
    assert result["added"][0]["row"] == {"snapshot_date": dt.date(2026, 9, 7), "id": 3, "status": "added"}
    assert result["removed"][0]["key"] == (2,)
    assert result["removed"][0]["row"] == {"snapshot_date": dt.date(2026, 9, 6), "id": 2, "status": "same"}
    assert result["changed"][0]["key"] == (1,)
    assert result["changed"][0]["columns"] == {"status": {"older": "old", "newer": "new"}}


def test_compare_rejects_same_date():
    with pytest.raises(ValueError, match="서로 다른"):
        compare.compare(FakeConnection(), "target", "orders", "2026-09-06", "2026-09-06")


def test_compare_limits_displayed_results(monkeypatch):
    def rows_with_many_changes(conn, sql, args=()):
        if "ENGINE" in sql:
            return [("InnoDB",)]
        if "information_schema.COLUMNS" in sql:
            return [
                ("snapshot_date", "date", "NO", None, None, ""),
                ("id", "int", "NO", None, None, ""),
                ("status", "varchar(10)", "NO", "utf8mb4", "utf8mb4_bin", ""),
            ]
        if "information_schema.STATISTICS" in sql:
            return [("PRIMARY", 0, 1, "snapshot_date", None), ("PRIMARY", 0, 2, "id", None)]
        if args[0] == dt.date(2026, 9, 6):
            return [(args[0], 1, "old"), (args[0], 2, "old")]
        return [(args[0], 1, "new"), (args[0], 3, "new")]

    monkeypatch.setattr(compare.db, "rows", rows_with_many_changes)
    result = compare.compare(
        FakeConnection(), "target", "orders", dt.date(2026, 9, 6), dt.date(2026, 9, 7), max_results=1
    )
    assert len(result["added"]) + len(result["removed"]) + len(result["changed"]) == 1
    assert result["results_truncated"] is True
