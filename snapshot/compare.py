"""Bounded, read-only comparison helpers for target snapshot tables."""

from __future__ import annotations

from . import db

MAX_ROWS = 5_000
MAX_CHANGES = 200
MAX_RESULTS = 1_000


def target_tables(conn, database):
    rows = db.rows(
        conn,
        "SELECT TABLE_NAME FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME",
        (database,),
    )
    return [row[0] for row in rows if not row[0].casefold().startswith(db.PREFIX.casefold())]


def _metadata(conn, database, table):
    db.ident(table)
    schema = db.schema(
        lambda sql, args=(): db.rows(conn, sql, args), database, table, include_indexes=True
    )
    columns = [column["name"] for column in schema["columns"]]
    if not any(name.casefold() == "snapshot_date" for name in columns):
        raise ValueError("스냅샷 날짜 컬럼이 없는 테이블입니다.")
    if not db.has_leading_index(schema, "snapshot_date"):
        raise ValueError("날짜별 비교에는 snapshot_date 선두 인덱스가 필요합니다.")
    key = [name for name in schema["key"] if name.casefold() != "snapshot_date"]
    if not key:
        raise ValueError("날짜별 비교에는 snapshot_date 외의 PK 또는 UNIQUE 키가 필요합니다.")
    return columns, key


def available_dates(conn, database, table):
    db.ident(table)
    _metadata(conn, database, table)
    return [
        row[0]
        for row in db.rows(
            conn,
            f"SELECT DISTINCT `snapshot_date` FROM {db.ident(table)} "
            "WHERE `snapshot_date` IS NOT NULL ORDER BY `snapshot_date` DESC LIMIT %s ROWS EXAMINED 10000",
            (1000,),
        )
    ]


def compare(conn, database, table, older, newer, max_rows=MAX_ROWS, max_results=MAX_CHANGES):
    if older == newer:
        raise ValueError("서로 다른 두 날짜를 선택하세요.")
    if type(max_rows) is not int or not 1 <= max_rows <= MAX_ROWS:
        raise ValueError(f"비교 행 수는 1~{MAX_ROWS:,} 범위여야 합니다.")
    if type(max_results) is not int or not 1 <= max_results <= MAX_RESULTS:
        raise ValueError(f"결과 최대 건수는 1~{MAX_RESULTS:,} 범위여야 합니다.")
    columns, key = _metadata(conn, database, table)
    names = ",".join(db.ident(name) for name in columns)
    order = ",".join(db.ident(name) for name in key)
    examined_limit = min(10_000, max_rows * 2 + 1)
    sql = (
        f"SELECT {names} FROM {db.ident(table)} WHERE `snapshot_date`=%s "
        f"ORDER BY {order} LIMIT %s ROWS EXAMINED {examined_limit}"
    )

    def load(date):
        rows = db.rows(conn, sql, (date, max_rows + 1))
        truncated = len(rows) > max_rows
        rows = rows[:max_rows]
        positions = [columns.index(name) for name in key]
        return {tuple(row[index] for index in positions): row for row in rows}, truncated

    old_rows, old_truncated = load(older)
    new_rows, new_truncated = load(newer)
    added_keys = sorted(new_rows.keys() - old_rows.keys(), key=repr)
    removed_keys = sorted(old_rows.keys() - new_rows.keys(), key=repr)
    added_all = [
        {"key": row_key, "row": dict(zip(columns, new_rows[row_key]))} for row_key in added_keys[:max_results]
    ]
    removed_all = [
        {"key": row_key, "row": dict(zip(columns, old_rows[row_key]))}
        for row_key in removed_keys[:max_results]
    ]
    changed_all = []
    for row_key in sorted(old_rows.keys() & new_rows.keys(), key=repr):
        old_row, new_row = old_rows[row_key], new_rows[row_key]
        differences = {
            name: {"older": old_row[i], "newer": new_row[i]}
            for i, name in enumerate(columns)
            if name.casefold() != "snapshot_date" and old_row[i] != new_row[i]
        }
        if differences and len(changed_all) < max_results:
            changed_all.append({"key": row_key, "columns": differences})
    remaining = max_results
    added = added_all[:remaining]
    remaining -= len(added)
    removed = removed_all[:remaining]
    remaining -= len(removed)
    changed = changed_all[:remaining]
    total_changes = len(added_keys) + len(removed_keys)
    total_changes += sum(
        1
        for row_key in old_rows.keys() & new_rows.keys()
        if any(
            name.casefold() != "snapshot_date" and old_rows[row_key][i] != new_rows[row_key][i]
            for i, name in enumerate(columns)
        )
    )
    return {
        "table": table,
        "older": older,
        "newer": newer,
        "max_rows": max_rows,
        "max_results": max_results,
        "older_count": len(old_rows),
        "newer_count": len(new_rows),
        "added": added,
        "removed": removed,
        "changed": changed,
        "truncated": old_truncated or new_truncated,
        "results_truncated": total_changes > max_results,
    }
