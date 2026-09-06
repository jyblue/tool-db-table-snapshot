"""Bounded, read-only comparison helpers for target snapshot tables."""

from __future__ import annotations

from . import db

MAX_ROWS = 5_000
MAX_CHANGES = 200


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
    schema = db.schema(lambda sql, args=(): db.rows(conn, sql, args), database, table)
    columns = [column["name"] for column in schema["columns"]]
    if "snapshot_date" not in columns:
        raise ValueError("스냅샷 날짜 컬럼이 없는 테이블입니다.")
    key = [name for name in schema["key"] if name != "snapshot_date"]
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
            "WHERE `snapshot_date` IS NOT NULL ORDER BY `snapshot_date` DESC LIMIT %s",
            (1000,),
        )
    ]


def compare(conn, database, table, older, newer, max_rows=MAX_ROWS):
    if older == newer:
        raise ValueError("서로 다른 두 날짜를 선택하세요.")
    if type(max_rows) is not int or not 1 <= max_rows <= MAX_ROWS:
        raise ValueError(f"비교 행 수는 1~{MAX_ROWS:,} 범위여야 합니다.")
    columns, key = _metadata(conn, database, table)
    names = ",".join(db.ident(name) for name in columns)
    order = ",".join(db.ident(name) for name in key)
    sql = f"SELECT {names} FROM {db.ident(table)} WHERE `snapshot_date`=%s ORDER BY {order} LIMIT %s"

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
    changed = []
    for row_key in sorted(old_rows.keys() & new_rows.keys(), key=repr):
        old_row, new_row = old_rows[row_key], new_rows[row_key]
        differences = {
            name: {"older": old_row[i], "newer": new_row[i]}
            for i, name in enumerate(columns)
            if name != "snapshot_date" and old_row[i] != new_row[i]
        }
        if differences and len(changed) < MAX_CHANGES:
            changed.append({"key": row_key, "columns": differences})
    return {
        "table": table,
        "older": older,
        "newer": newer,
        "older_count": len(old_rows),
        "newer_count": len(new_rows),
        "added": added_keys[:MAX_CHANGES],
        "removed": removed_keys[:MAX_CHANGES],
        "changed": changed,
        "truncated": old_truncated or new_truncated,
    }
