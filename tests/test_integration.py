"""SNAPSHOT_TEST_PORT points exclusively to the disposable test server."""

import datetime as dt
import os
import time
from decimal import Decimal

import pymysql
import pytest

from snapshot import db, process
from snapshot.engine import Engine, build_spec, reconcile
from snapshot.store import Store

pytestmark = pytest.mark.integration


@pytest.fixture
def env(tmp_path):
    port = os.environ.get("SNAPSHOT_TEST_PORT")
    if not port:
        pytest.skip("Set SNAPSHOT_TEST_PORT for disposable MariaDB integration tests")
    root = pymysql.connect(
        host="127.0.0.1", port=int(port), user="root", password="snapshot-test-only", autocommit=True
    )
    with root.cursor() as c:
        c.execute("DROP DATABASE IF EXISTS snapshot_source")
        c.execute("DROP DATABASE IF EXISTS snapshot_target")
        c.execute("CREATE DATABASE snapshot_source CHARACTER SET utf8mb4")
        c.execute("CREATE DATABASE snapshot_target CHARACTER SET utf8mb4")
        c.execute("CREATE USER IF NOT EXISTS 'snapshot_reader'@'%' IDENTIFIED BY 'read-test-only'")
        c.execute("GRANT SELECT ON snapshot_source.* TO 'snapshot_reader'@'%'")
        c.execute(
            "CREATE TABLE snapshot_source.records (a INT NOT NULL,b INT NOT NULL, s LONGTEXT NULL, amount DECIMAL(30,8), payload LONGBLOB, stamp DATETIME(6), duration TIME(6), generated_value INT GENERATED ALWAYS AS (a+b) STORED, PRIMARY KEY(a,b)) ENGINE=InnoDB"
        )
        values = [
            (
                i // 5,
                i % 5,
                None if i % 7 == 0 else "\n\t\\한글😀",
                Decimal("123456789012345.12345678"),
                b"\x00\xff\t\n",
                dt.datetime(2026, 1, 2, 3, 4, 5, 123456),
                dt.timedelta(hours=-30, microseconds=123456),
            )
            for i in range(1205)
        ]
        c.executemany(
            "INSERT INTO snapshot_source.records(a,b,s,amount,payload,stamp,duration) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            values,
        )
        c.execute("CREATE TABLE snapshot_source.empty (id INT PRIMARY KEY) ENGINE=InnoDB")
        c.execute("CREATE TABLE snapshot_source.no_key (s TEXT) ENGINE=InnoDB")
        c.execute("INSERT INTO snapshot_source.no_key VALUES (NULL),(''),('한글')")
    store = Store(tmp_path / "state")
    src = dict(
        id="s",
        name="source",
        role="source",
        host="127.0.0.1",
        port=int(port),
        database="snapshot_source",
        user="snapshot_reader",
        tls=False,
    )
    tgt = dict(
        id="t",
        name="target",
        role="target",
        host="127.0.0.1",
        port=int(port),
        database="snapshot_target",
        user="root",
        tls=False,
    )
    store.save("connection_profile", src)
    store.save("connection_profile", tgt)
    j = dict(
        id="j",
        name="Records",
        source_id="s",
        target_id="t",
        source_table="records",
        target_table="records",
        read_mode="pk",
        batch_rows=101,
        wait_ms=0,
        connect_timeout=5,
        read_timeout=10,
        write_timeout=10,
        retries=0,
    )
    store.save("backup_job", j)
    yield store, root, j, {"s": "read-test-only", "t": "snapshot-test-only"}, tmp_path / "files"
    root.close()


def run(env, ids=["j"], date="2026-09-06", limit=None, engine_cls=Engine, retry_of=None):
    store, root, j, secrets, path = env
    spec = build_spec(store, ids, date, limit, path)
    rid = store.queue(spec, retry_of)
    store.update_run(rid, state="RUNNING", started=time.time())
    engine_cls(store, rid, secrets).execute()
    return store.run(rid)


def query(root, sql):
    with root.cursor() as c:
        c.execute(sql)
        return c.fetchall()


def test_round_trip_dates_replacement_and_readonly(env):
    store, root, j, secrets, path = env
    first = run(env)
    assert first["state"] == "SUCCESS", first["error"]
    original = query(root, "SELECT * FROM snapshot_source.records ORDER BY a,b")
    copied = query(
        root,
        "SELECT a,b,s,amount,payload,stamp,duration,generated_value FROM snapshot_target.records ORDER BY a,b",
    )
    assert copied == original
    assert run(env, date="2026-09-07")["state"] == "SUCCESS"
    query(root, "DELETE FROM snapshot_source.records WHERE a=0")
    assert run(env)["state"] == "SUCCESS"
    assert query(
        root, "SELECT snapshot_date,COUNT(*) FROM snapshot_target.records GROUP BY snapshot_date"
    ) == ((dt.date(2026, 9, 6), 1200), (dt.date(2026, 9, 7), 1205))
    assert query(root, "SELECT COUNT(*) FROM snapshot_target._snapshot_runs")[0][0] == 3


@pytest.mark.parametrize("limit", [100, 1000, 27])
def test_test_limit_does_not_create_formal_table(env, limit):
    result = run(env, limit=limit)
    assert result["state"] == "SUCCESS", result["error"]
    assert env[0].tables(result["id"])[0]["extracted"] == limit
    assert (
        query(
            env[1],
            "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='snapshot_target' AND TABLE_NAME='records'",
        )[0][0]
        == 0
    )


def test_empty_stream_and_no_key_rejection(env):
    s, root, j, _, _ = env
    s.save("backup_job", dict(j, id="e", source_table="empty", target_table="empty"))
    s.save("backup_job", dict(j, id="n", source_table="no_key", target_table="no_key", read_mode="stream"))
    result = run(env, ["e", "n"])
    assert result["state"] == "SUCCESS", result["error"]
    assert query(root, "SELECT COUNT(*) FROM snapshot_target.empty")[0][0] == 0
    assert query(root, "SELECT COUNT(*) FROM snapshot_target.no_key")[0][0] == 3
    s.save("backup_job", dict(j, id="n", source_table="no_key", target_table="no_key"))
    assert run(env, ["n"])["state"] == "FAILED"


def test_failed_load_reuses_file_and_preserves_snapshot(env):
    s, root, j, secrets, path = env
    assert run(env)["state"] == "SUCCESS"
    query(root, "DELETE FROM snapshot_source.records WHERE a=0")

    class FailLoad(Engine):
        def load(self, *args):
            raise ValueError("injected load failure")

    result = run(env, engine_cls=FailLoad)
    assert result["state"] == "FAILED"
    assert query(root, "SELECT COUNT(*) FROM snapshot_target.records")[0][0] == 1205

    class NoExtract(Engine):
        def extract(self, *args):
            raise AssertionError("retry must reuse completed file")

    retry = run(env, engine_cls=NoExtract, retry_of=result["id"])
    assert retry["state"] == "SUCCESS", retry["error"]
    assert query(root, "SELECT COUNT(*) FROM snapshot_target.records")[0][0] == 1200


def test_later_table_failure_preserves_entire_bundle(env):
    s, root, j, _, _ = env
    s.save("backup_job", dict(j, id="e", source_table="empty", target_table="empty"))
    assert run(env, ["j", "e"])["state"] == "SUCCESS"
    query(root, "DELETE FROM snapshot_source.records")

    class FailSecond(Engine):
        def load(self, *args):
            if self.current == 1:
                raise ValueError("second table failed")
            return super().load(*args)

    result = run(env, ["j", "e"], engine_cls=FailSecond)
    assert result["state"] == "FAILED"
    assert query(root, "SELECT COUNT(*) FROM snapshot_target.records")[0][0] == 1205


def test_cancel_preserves_old_snapshot(env):
    assert run(env)["state"] == "SUCCESS"

    class CancelLoad(Engine):
        def load(self, *args):
            super().load(*args)
            self.store.cancel(self.run_id)

    result = run(env, engine_cls=CancelLoad)
    assert result["state"] == "CANCELLED"
    assert query(env[1], "SELECT COUNT(*) FROM snapshot_target._snapshot_runs")[0][0] == 1


def test_committed_marker_reconciles_sqlite(env):
    result = run(env)
    assert result["state"] == "SUCCESS"
    s = env[0]
    s.update_run(result["id"], state="INTERRUPTED", publish_pending=1)
    assert reconcile(s, result["id"], env[3]) == "SUCCESS"
    assert not s.run(result["id"])["publish_pending"]


def test_real_worker_process(env):
    s, root, j, secrets, path = env
    spec = build_spec(s, ["j"], "2026-09-06", 100, path)
    rid = process.start(s, spec, secrets)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = s.run(rid)
        if result["state"] in ("SUCCESS", "FAILED", "INTERRUPTED"):
            break
        time.sleep(0.1)
    assert result["state"] == "SUCCESS", result["error"]
    assert result["pid"] and result["process_created"]


@pytest.mark.parametrize("commit_first", [False, True])
def test_publication_connection_loss_resolves_by_marker(env, commit_first):
    assert run(env)["state"] == "SUCCESS"
    query(env[1], "DELETE FROM snapshot_source.records WHERE a=0")

    class LostCommit(Engine):
        def target(self):
            conn = super().target()
            engine = self

            class Wrapped:
                def __getattr__(self, name):
                    return getattr(conn, name)

                def commit(self):
                    if engine.store.run(engine.run_id)["state"] == "PUBLISHING":
                        if commit_first:
                            conn.commit()
                        conn.close()
                        raise pymysql.OperationalError(2013, "injected lost commit response")
                    return conn.commit()

            return Wrapped()

    result = run(env, engine_cls=LostCommit)
    assert result["state"] == ("SUCCESS" if commit_first else "INTERRUPTED"), result["error"]
    assert not result["publish_pending"]
    assert query(env[1], "SELECT COUNT(*) FROM snapshot_target.records")[0][0] == (
        1200 if commit_first else 1205
    )


def test_disk_full_keeps_partial_and_old_snapshot(env, monkeypatch):
    from snapshot import codec

    assert run(env)["state"] == "SUCCESS"
    original = codec.Writer.write

    def fail(writer, rows):
        original(writer, rows)
        raise OSError(28, "injected disk full")

    monkeypatch.setattr(codec.Writer, "write", fail)
    result = run(env)
    assert result["state"] == "FAILED"
    assert query(env[1], "SELECT COUNT(*) FROM snapshot_target.records")[0][0] == 1205
    assert env[0].tables(result["id"])[0]["file"] is None
    assert list((env[4] / result["id"]).glob("*.partial"))


@pytest.mark.parametrize("force", [False, True])
def test_actual_worker_cancel_and_force_stop(env, force):
    s, root, j, secrets, path = env
    s.save("backup_job", dict(j, wait_ms=5000))
    spec = build_spec(s, ["j"], "2026-09-06", None, path)
    rid = process.start(s, spec, secrets)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if s.tables(rid)[0]["stage"] == "SLEEPING":
            break
        time.sleep(0.05)
    assert s.tables(rid)[0]["stage"] == "SLEEPING"
    if force:
        process.force_stop(s, rid)
    else:
        s.cancel(rid)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = s.run(rid)
        if result["state"] in ("CANCELLED", "INTERRUPTED"):
            break
        time.sleep(0.05)
    assert result["state"] == ("INTERRUPTED" if force else "CANCELLED"), result
    assert query(root, "SELECT COUNT(*) FROM snapshot_target.records")[0][0] == 0
    if force:
        assert result["server_status"] == "로컬 종료 완료 / 서버 종료 미확인"
        assert not process.owned_process(result)
    process.cleanup(s, rid, secrets)


def test_twenty_tables_one_source_connection(env, monkeypatch):
    s, root, j, _, _ = env
    for i in range(20):
        s.save("backup_job", dict(j, id=f"j{i}", source_table="empty", target_table=f"table_{i}"))
    original_init = db.Source.__init__
    original_close = db.Source.close
    live = set()
    peak = 0

    def init(self, *args, **kwargs):
        nonlocal peak
        original_init(self, *args, **kwargs)
        live.add(id(self))
        peak = max(peak, len(live))

    def close(self):
        live.discard(id(self))
        return original_close(self)

    monkeypatch.setattr(db.Source, "__init__", init)
    monkeypatch.setattr(db.Source, "close", close)
    result = run(env, [f"j{i}" for i in range(20)])
    assert result["state"] == "SUCCESS", result["error"]
    assert peak == 1 and not live
    assert len(env[0].tables(result["id"])) == 20


def test_extract_disconnect_restarts_from_beginning(env, monkeypatch):
    from contextlib import contextmanager

    original = db.Source.select
    failed = False

    @contextmanager
    def select(self, sql, args=(), streaming=False):
        nonlocal failed
        with original(self, sql, args, streaming) as cursor:
            if streaming and not failed:
                original_fetch = cursor.fetchmany
                calls = 0

                def fetch(size):
                    nonlocal failed, calls
                    calls += 1
                    if calls == 2:
                        failed = True
                        raise pymysql.OperationalError(2013, "injected extraction disconnect")
                    return original_fetch(size)

                cursor.fetchmany = fetch
            yield cursor

    s, root, j, _, _ = env
    s.save("backup_job", dict(j, retries=1))
    monkeypatch.setattr(db.Source, "select", select)
    result = run(env)
    assert result["state"] == "SUCCESS", result["error"]
    assert failed
    assert query(root, "SELECT COUNT(*) FROM snapshot_target.records")[0][0] == 1205


def test_target_schema_mismatch_keeps_data(env):
    assert run(env)["state"] == "SUCCESS"
    query(env[1], "ALTER TABLE snapshot_target.records ADD COLUMN unexpected INT")
    result = run(env)
    assert result["state"] == "FAILED" and "스키마 불일치" in result["error"]
    assert query(env[1], "SELECT COUNT(*) FROM snapshot_target.records")[0][0] == 1205


def test_load_disconnect_rebuilds_staging(env):
    s, root, j, _, _ = env
    s.save("backup_job", dict(j, retries=1))

    class LostLoad(Engine):
        lost = False

        def target(self):
            conn = super().target()
            engine = self

            class Wrapped:
                def __getattr__(self, name):
                    return getattr(conn, name)

                def commit(self):
                    if (
                        engine.current is not None
                        and engine.store.tables(engine.run_id)[engine.current]["stage"] == "LOADING"
                        and not engine.lost
                    ):
                        engine.lost = True
                        conn.close()
                        raise pymysql.OperationalError(2013, "injected load connection loss")
                    return conn.commit()

            return Wrapped()

    result = run(env, engine_cls=LostLoad)
    assert result["state"] == "SUCCESS", result["error"]
    assert query(root, "SELECT COUNT(*) FROM snapshot_target.records")[0][0] == 1205


def test_same_physical_schema_blocks_before_target_ddl(env):
    s, root, j, _, _ = env
    target = next(p for p in s.profiles() if p["id"] == "t")
    s.save("connection_profile", dict(target, database="snapshot_source", host="localhost"))
    result = run(env)
    assert result["state"] == "FAILED" and "같은 서버" in result["error"]
    assert (
        query(
            root,
            "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='snapshot_source' AND TABLE_NAME='_snapshot_runs'",
        )[0][0]
        == 0
    )


def test_incoming_fk_blocks_cascading_writes(env):
    assert run(env)["state"] == "SUCCESS"
    query(
        env[1],
        "CREATE TABLE snapshot_target.child (d DATE,a INT,b INT,FOREIGN KEY (d,a,b) REFERENCES snapshot_target.records(snapshot_date,a,b) ON DELETE CASCADE) ENGINE=InnoDB",
    )
    result = run(env)
    assert result["state"] == "FAILED" and "FK" in result["error"]
    assert query(env[1], "SELECT COUNT(*) FROM snapshot_target.records")[0][0] == 1205


def test_nonempty_ui_monitor(env, monkeypatch):
    from pathlib import Path

    from streamlit.testing.v1 import AppTest

    result = run(env, limit=100)
    assert result["state"] == "SUCCESS"
    monkeypatch.setenv("SNAPSHOT_HOME", str(env[0].root))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=20)
    assert not app.exception
    assert any("완료" in text.value for text in app.subheader)


def test_bit_enum_set_auto_increment_timestamp_and_unique_key(env):
    s, root, j, _, _ = env
    query(
        root,
        "CREATE TABLE snapshot_source.types (id INT PRIMARY KEY AUTO_INCREMENT,bits BIT(16),flag BIT(1),options SET('a','b'),status ENUM('x','y'),ts TIMESTAMP(6),raw VARBINARY(10),y YEAR,f DOUBLE) ENGINE=InnoDB",
    )
    query(
        root,
        "INSERT INTO snapshot_source.types VALUES (1,b'1000000000000001',b'1','a,b','y','2026-01-02 03:04:05.123456',X'00FF',2026,1.23456789012345)",
    )
    query(
        root, "CREATE TABLE snapshot_source.unique_key (k VARCHAR(20) NOT NULL UNIQUE, v INT) ENGINE=InnoDB"
    )
    query(root, "INSERT INTO snapshot_source.unique_key VALUES ('a',1),('b',2),('c',3)")
    s.save("backup_job", dict(j, id="types", source_table="types", target_table="types"))
    s.save(
        "backup_job", dict(j, id="unique", source_table="unique_key", target_table="unique_key", batch_rows=1)
    )
    result = run(env, ["types", "unique"])
    assert result["state"] == "SUCCESS", result["error"]
    assert query(root, "SELECT id,bits,flag,options,status,ts,raw,y,f FROM snapshot_target.types") == query(
        root, "SELECT * FROM snapshot_source.types"
    )
    assert query(root, "SELECT k,v FROM snapshot_target.unique_key ORDER BY k") == query(
        root, "SELECT * FROM snapshot_source.unique_key ORDER BY k"
    )


def test_target_connection_test_checks_source_alias_first(env):
    profiles = {p["id"]: p for p in env[0].profiles()}
    target = dict(profiles["t"], database=profiles["s"]["database"])
    with pytest.raises(ValueError, match="같은 서버"):
        db.test_connection(target, env[3]["t"], [profiles["s"]], lambda p: env[3][p["id"]])
