import datetime as dt
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from snapshot import codec, db
from snapshot.secrets import safe_error
from snapshot.store import Store


def test_codec_preserves_values_and_rejects_corruption(tmp_path):
    values = [
        None,
        "",
        "\n\t\\한글😀",
        b"\x00\xff",
        Decimal("123456789012345.000001"),
        dt.datetime(2026, 1, 2, 3, 4, 5, 678901),
        dt.date(2026, 1, 2),
        dt.timedelta(hours=-30, microseconds=1),
    ]
    p = tmp_path / "0.jsonl"
    w = codec.Writer(p)
    w.write([values])
    w.complete({"run_id": "r"})
    assert codec.verify(p, {"run_id": "r"})["rows"] == 1
    assert list(codec.batches(p, 100))[0][0] == tuple(values)
    p.write_bytes(p.read_bytes() + b"[]\n")
    with pytest.raises(ValueError, match="무결성"):
        codec.verify(p, {"run_id": "r"})


def test_partial_file_not_complete(tmp_path):
    p = tmp_path / "0.jsonl"
    w = codec.Writer(p)
    w.write([(1,)])
    w.close()
    assert not p.exists()
    with pytest.raises(FileNotFoundError):
        codec.verify(p, {})


def test_composite_keyset_and_identifier():
    where, args = db.keyset(["a", "b", "c"], [1, 2, 3])
    assert where == " WHERE ((`a`>%s) OR (`a`=%s AND `b`>%s) OR (`a`=%s AND `b`=%s AND `c`>%s))"
    assert args == [1, 1, 2, 1, 2, 3]
    assert db.ident("a`b") == "`a``b`"
    with pytest.raises(ValueError):
        db.ident("a\0b")
    sql, args = db.read_sql("t", [{"name": "id"}], ["id"], None, 100)
    assert sql.endswith("ORDER BY `id` LIMIT %s") and args == [100]
    assert "OFFSET" not in sql


def test_source_rejects_write_without_connection():
    src = object.__new__(db.Source)
    with pytest.raises(ValueError, match="SELECT"):
        src.rows("DELETE FROM x")


def test_duplicate_start_is_atomic_and_pending_blocks(tmp_path):
    s = Store(tmp_path)
    spec = {"jobs": [{"id": "j"}]}

    def launch(_):
        try:
            return s.queue(spec)
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(launch, range(8)))
    assert len([r for r in results if r]) == 1
    rid = next(r for r in results if r)
    s.update_run(rid, state="INTERRUPTED", publish_pending=1)
    with pytest.raises(ValueError):
        s.queue(spec)
    s.update_run(rid, publish_pending=0)
    assert s.queue(spec)


def test_cancel_blocks_publish(tmp_path):
    s = Store(tmp_path)
    r = s.queue({"jobs": [{"id": "j"}]})
    s.cancel(r)
    assert not s.begin_publish(r)
    assert not s.run(r)["publish_pending"]


def test_profiles_reject_password_and_freeze_edits(tmp_path):
    s = Store(tmp_path)
    with pytest.raises(ValueError):
        s.save("connection_profile", {"password": "secret"})
    s.queue({"jobs": [{"id": "j"}]})
    with pytest.raises(ValueError):
        s.save("backup_job", {"name": "edited"})


def test_source_target_alias_detection():
    p = {"database": "db", "host": "localhost", "port": 3306}
    with pytest.raises(ValueError, match="같은 서버"):
        db.assert_distinct(
            p, dict(p, host="127.0.0.1"), ("host", 3306, 1, "/data"), ("host", 3306, 1, "/data")
        )


def test_generated_column_and_snapshot_conflict():
    col = {
        "name": "id",
        "type": "int(11)",
        "nullable": "NO",
        "charset": None,
        "collation": None,
        "extra": "auto_increment",
    }
    schema = {"columns": [col], "pk": ["id"], "key": ["id"], "unique": {"PRIMARY": ["id"]}}
    expected = db.expected_schema(schema)
    ddl = db.create_sql("target", expected)
    assert "AUTO_INCREMENT" not in ddl
    assert "PRIMARY KEY (`snapshot_date`,`id`)" in ddl
    db.compare_schema(expected, expected)
    with pytest.raises(ValueError, match="스키마"):
        db.compare_schema(dict(expected, pk=["id"]), expected)
    with pytest.raises(ValueError, match="충돌"):
        db.expected_schema(dict(schema, columns=[dict(col, name="SNAPSHOT_DATE")]))


def test_error_redaction():
    import pymysql

    assert "top-secret" not in safe_error(ValueError("top-secret"), ["top-secret"])
    assert "private-row" not in safe_error(pymysql.DataError(1406, "private-row"))


def test_recovery_cannot_overwrite_success_or_new_worker(tmp_path):
    store = Store(tmp_path)
    rid = store.queue({"jobs": [{"id": "j"}]})
    old = store.run(rid)
    store.update_run(rid, pid=123, process_created=123.0, state="RUNNING")
    assert not store.interrupt_if_current(old)
    observed = store.run(rid)
    store.update_run(rid, state="SUCCESS")
    assert not store.interrupt_if_current(observed)
    assert store.run(rid)["state"] == "SUCCESS"


def test_negative_time_binding():
    assert db.bind_value(dt.timedelta(hours=-30, microseconds=1)) == "-29:59:59.999999"
    assert db.bind_value(dt.timedelta(hours=30, microseconds=1)) == "30:00:00.000001"


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM records",
        "UPDATE records SET a=0",
        "TRUNCATE records",
        "DROP TABLE records",
        "INSERT INTO records VALUES (1)",
        "ALTER TABLE records ADD x INT",
        "CALL dangerous()",
        "SET SESSION TRANSACTION READ WRITE",
        "START TRANSACTION READ WRITE",
        "SELECT 1",
        "SELECT VERSION(); DROP TABLE records",
        "SELECT * FROM records INTO OUTFILE '/tmp/export'",
        "SELECT * FROM records FOR UPDATE",
        "SELECT * FROM records LOCK IN SHARE MODE",
        "SELECT dangerous()",
        "SELECT SLEEP(10)",
        "SELECT VERSION() /* extra */",
    ],
)
def test_source_accepts_only_fixed_metadata_templates(sql):
    source = object.__new__(db.Source)
    with pytest.raises(ValueError, match="템플릿"):
        source.rows(sql)


@pytest.mark.parametrize("failure", ["set", "verify", "disabled"])
def test_source_initialization_fails_closed(monkeypatch, failure):
    from unittest.mock import MagicMock

    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchone.return_value = (0,)
    if failure == "set":
        cursor.execute.side_effect = RuntimeError("setup failed")
    elif failure == "verify":

        def execute(sql):
            if sql.startswith("SELECT"):
                raise RuntimeError("verification failed")

        cursor.execute.side_effect = execute
    monkeypatch.setattr(db.pymysql, "connect", lambda **options: conn)
    profile = dict(role="source", name="test", host="localhost", port=3306, database="test", user="test")
    with pytest.raises((RuntimeError, ValueError)):
        db.Source(profile, "unused")
    conn.close.assert_called_once()


@pytest.mark.parametrize("limit", [None, 0, 1001, True])
def test_source_rejects_unbounded_reads_before_connecting(limit):
    source = object.__new__(db.Source)
    with pytest.raises(ValueError, match="유한 배치"):
        with source.read("records", [{"name": "id"}], ["id"], limit=limit):
            pass
