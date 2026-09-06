"""SNAPSHOT_TEST_PORT points exclusively to the disposable test server."""

import datetime as dt
import os
import time
from decimal import Decimal

import pymysql
import pytest

from snapshot import compare, db, process
from snapshot.engine import Engine, build_spec, reconcile
from snapshot.store import Store

pytestmark = pytest.mark.integration


@pytest.fixture
def env(tmp_path):
    port = os.environ.get("SNAPSHOT_TEST_PORT")
    target_port = os.environ.get("SNAPSHOT_TEST_TARGET_PORT")
    if not port or not target_port:
        pytest.skip("Set SNAPSHOT_TEST_PORT and SNAPSHOT_TEST_TARGET_PORT to separate disposable servers")
    root = pymysql.connect(
        host="127.0.0.1", port=int(port), user="root", password="snapshot-test-only", autocommit=True
    )
    root.target_conn = pymysql.connect(
        host="127.0.0.1", port=int(target_port), user="root", password="snapshot-test-only", autocommit=True
    )
    query(root.target_conn, "DROP DATABASE IF EXISTS snapshot_target")
    query(root.target_conn, "CREATE DATABASE snapshot_target CHARACTER SET utf8mb4")
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
        port=int(target_port),
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
    root.target_conn.close()
    root.close()


def run(env, ids=["j"], date="2026-09-06", limit=None, engine_cls=Engine, retry_of=None):
    store, root, j, secrets, path = env
    spec = build_spec(store, ids, date, limit, path)
    rid = store.queue(spec, retry_of)
    store.update_run(rid, state="RUNNING", started=time.time())
    engine_cls(store, rid, secrets).execute()
    return store.run(rid)


def query(root, sql):
    if hasattr(root, "target_conn") and "snapshot_target" in sql:
        root = root.target_conn
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
    result = run(env)
    assert result["state"] == "SUCCESS", result["error"]
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
    assert run(env, ["e"])["state"] == "SUCCESS"
    result = run(env, ["n"])
    assert result["state"] == "FAILED" and "키 없는 전체 읽기" in result["error"]
    assert run(env, ["n"], limit=1000)["state"] == "SUCCESS"
    assert run(env, ["n"], limit=1001)["state"] == "FAILED"


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
    deadline = time.monotonic() + 10
    while process.owned_process(s.run(rid)) and time.monotonic() < deadline:
        time.sleep(0.05)
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


def test_extract_disconnect_stops_without_automatic_rescan(env, monkeypatch):
    from contextlib import contextmanager

    original = db.Source.read
    failed = False

    @contextmanager
    def read(self, *args, **kwargs):
        nonlocal failed
        with original(self, *args, **kwargs) as cursor:
            if not failed:
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
    monkeypatch.setattr(db.Source, "read", read)
    result = run(env)
    assert result["state"] == "FAILED", result["error"]
    assert failed
    assert query(root, "SELECT COUNT(*) FROM snapshot_target.records")[0][0] == 0


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
    s.save("connection_profile", dict(target, database="snapshot_source", host="localhost", port=root.port))
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
    target = dict(profiles["t"], database=profiles["s"]["database"], port=profiles["s"]["port"])
    with pytest.raises(ValueError, match="같은 서버"):
        db.test_connection(target, env[3]["t"], [profiles["s"]], lambda p: env[3][p["id"]])


def test_privileged_source_is_read_only_on_every_connection(env):
    store, root, _, secrets, _ = env
    profile = dict(next(p for p in store.profiles() if p["id"] == "s"), user="root")
    store.save("connection_profile", profile)
    secrets["s"] = "snapshot-test-only"
    writes = [
        "DELETE FROM records",
        "UPDATE records SET s='changed'",
        "TRUNCATE TABLE records",
        "DROP TABLE records",
        "INSERT INTO empty VALUES (1)",
        "ALTER TABLE empty ADD n INT",
        "CREATE TABLE forbidden (id INT)",
    ]
    # Bypass the app guard deliberately to verify MariaDB's second layer, including DDL.
    for _ in range(2):
        source = db.Source(profile, secrets["s"])
        try:
            assert query(source._conn, "SELECT @@session.tx_read_only") == ((1,),)
            for sql in writes:
                with pytest.raises(pymysql.OperationalError) as error:
                    query(source._conn, sql)
                assert error.value.args[0] == 1792
            with source.read("records", [{"name": "a"}], limit=1) as result:
                assert result.fetchmany(1) == [(0,)]
                assert not hasattr(result, "execute") and not hasattr(result, "connection")
        finally:
            source.close()
    result = run(env)
    assert result["state"] == "SUCCESS", result["error"]
    assert query(root, "SELECT COUNT(*) FROM snapshot_source.records") == ((1205,),)


def test_target_row_lock_timeout_rolls_back_entire_publication(env):
    store, root, job, _, _ = env
    store.save("backup_job", dict(job, id="second", target_table="records_second"))
    assert run(env, ["j", "second"])["state"] == "SUCCESS"
    query(root, "DELETE FROM snapshot_source.records WHERE a=0")

    class ShortLockWait(Engine):
        def target(self):
            conn = super().target()
            # Test-only bound; production currently inherits the server default.
            query(conn, "SET SESSION innodb_lock_wait_timeout=1")
            return conn

    root.target_conn.begin()
    try:
        query(root, "SELECT * FROM snapshot_target.records_second WHERE a=0 FOR UPDATE")
        result = run(env, ["j", "second"], engine_cls=ShortLockWait)
    finally:
        root.target_conn.rollback()
    assert result["state"] == "INTERRUPTED" and not result["publish_pending"]
    assert "1205" in result["error"]
    for table in ("records", "records_second"):
        assert query(root, f"SELECT COUNT(*) FROM snapshot_target.{table}") == ((1205,),)
    assert query(root, "SELECT COUNT(*) FROM snapshot_target._snapshot_runs") == ((1,),)


def test_target_without_date_index_is_currently_accepted(env):
    # Characterization of an open load risk, not an assertion that this is safe.
    store, root, job, _, _ = env
    expected = db.expected_schema(
        db.schema(lambda sql, args: db.rows(root, sql, args), "snapshot_source", "no_key")
    )
    query(root, "USE snapshot_target")
    db.prepare(root.target_conn, "snapshot_target", "no_key", expected)
    query(root, "ALTER TABLE snapshot_target.no_key DROP INDEX snapshot_date_idx")
    db.prepare(
        root.target_conn, "snapshot_target", "no_key", expected
    )  # Non-unique index is not checked yet.
    with root.target_conn.cursor(pymysql.cursors.DictCursor) as cursor:
        cursor.execute("EXPLAIN DELETE FROM snapshot_target.no_key WHERE snapshot_date='2026-09-06'")
        plan = cursor.fetchone()
    assert plan["type"] == "ALL" and plan["key"] is None


def test_target_load_batches_but_publication_is_one_statement(env, monkeypatch):
    store, _, job, _, _ = env
    store.save("backup_job", dict(job, batch_rows=100000))
    batches, publications = [], []
    original_many = db.StrictCursor.executemany
    original_execute = pymysql.cursors.Cursor.execute

    def many(self, sql, args):
        batches.append(len(args))
        return original_many(self, sql, args)

    def execute(self, sql, args=None):
        if isinstance(sql, str) and sql.startswith("INSERT INTO `records` SELECT"):
            publications.append(sql)
        return original_execute(self, sql, args)

    monkeypatch.setattr(db.StrictCursor, "executemany", many)
    monkeypatch.setattr(pymysql.cursors.Cursor, "execute", execute)
    assert run(env)["state"] == "SUCCESS"
    assert batches == [1000, 205]
    assert len(publications) == 1  # Publication does not inherit the load batch limit.


@pytest.fixture
def protected_source(env):
    profile = next(p for p in env[0].profiles() if p["id"] == "s")
    source = db.Source(profile, env[3]["s"])
    try:
        yield source
    finally:
        source.close()


def test_source_server_timeout_and_mutex(env, protected_source):
    source = protected_source
    started = time.monotonic()
    # Bypass templates only in this isolated test to prove the server-side bound.
    with pytest.raises(pymysql.OperationalError) as error:
        query(source._conn, "SELECT SLEEP(10)")
    assert error.value.args[0] == 1969
    assert time.monotonic() - started < 5
    profile = next(p for p in env[0].profiles() if p["id"] == "s")
    with pytest.raises(ValueError, match="다른 스냅샷"):
        db.Source(profile, env[3]["s"])
    source.close()
    replacement = db.Source(profile, env[3]["s"])
    replacement.close()


def test_source_read_does_not_wait_for_row_writer_or_hold_mdl(env, protected_source):
    root, source = env[1], protected_source
    columns = [{"name": "a"}, {"name": "b"}, {"name": "s"}]
    root.begin()
    try:
        query(root, "UPDATE snapshot_source.records SET s='uncommitted' WHERE a=0 AND b=0")
        with source.read("records", columns, ["a", "b"], limit=10) as result:
            assert result.fetchmany(1)[0][2] is None  # Original committed value; no row-lock wait.
    finally:
        root.rollback()
    query(root, "SET SESSION lock_wait_timeout=1")
    with source.read("records", columns, ["a", "b"], limit=10) as result:
        # Local consumer has not fetched yet, but the source transaction is already finished.
        query(root, "ALTER TABLE snapshot_source.records ADD COLUMN safety_probe INT NULL")
        assert len(result.fetchall()) == 10


def test_source_metadata_lock_wait_is_bounded(env, protected_source):
    root = env[1]
    query(root, "LOCK TABLES snapshot_source.records WRITE")
    started = time.monotonic()
    try:
        with pytest.raises(pymysql.OperationalError) as error:
            with protected_source.read("records", [{"name": "a"}], ["a"], limit=10):
                pass
        assert error.value.args[0] in (1205, 1969)
        assert time.monotonic() - started < 5
    finally:
        query(root, "UNLOCK TABLES")


def test_source_rejects_unindexed_plan_and_large_result(env, protected_source):
    root, source = env[1], protected_source
    with pytest.raises(ValueError, match="인덱스"):
        with source.read("records", [{"name": "s"}], ["s"], limit=10):
            pass
    query(root, "UPDATE snapshot_source.records SET s=REPEAT('x', 1024*1024) WHERE a<2")
    with pytest.raises(ValueError, match="상한"):
        with source.read("records", [{"name": "a"}, {"name": "b"}, {"name": "s"}], ["a", "b"], limit=10):
            pass
    assert not source._conn.open


def test_source_examined_limit_never_returns_partial_success(env, protected_source):
    query(env[1], "INSERT INTO snapshot_source.no_key SELECT 'x' FROM snapshot_source.seq_1_to_3000")
    # A generated query's examined-row ceiling can also be reached under concurrent churn.
    with pytest.raises((ValueError, pymysql.OperationalError)):
        with protected_source._select(
            "SELECT s FROM no_key ORDER BY s LIMIT 1000 ROWS EXAMINED 10", streaming=True
        ):
            pytest.fail("Partial result must not reach a caller")


def test_target_date_compare_reports_changed_and_removed_rows(env):
    store, root, _, _, _ = env
    assert run(env, date="2026-09-06")["state"] == "SUCCESS"
    query(root, "UPDATE snapshot_source.records SET s='changed' WHERE a=0 AND b=0")
    query(root, "DELETE FROM snapshot_source.records WHERE a=0 AND b=1")
    assert run(env, date="2026-09-07")["state"] == "SUCCESS"
    root.target_conn.select_db("snapshot_target")
    result = compare.compare(
        root.target_conn,
        "snapshot_target",
        "records",
        dt.date(2026, 9, 6),
        dt.date(2026, 9, 7),
    )
    assert result["older_count"] == 1205
    assert result["newer_count"] == 1204
    assert result["removed"] == [(0, 1)]
    assert result["changed"][0]["key"] == (0, 0)


def test_shared_instance_is_rejected_even_with_different_schema(env):
    store, root, _, _, _ = env
    target = next(p for p in store.profiles() if p["id"] == "t")
    store.save("connection_profile", dict(target, port=root.port))
    result = run(env)
    assert result["state"] == "FAILED" and "같은 서버" in result["error"]
    assert query(root, "SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='snapshot_source'")
    with root.cursor() as cursor:
        cursor.execute("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA='snapshot_target'")
        assert cursor.fetchone() == (0,)  # No writes on the source instance's target schema.


def test_source_read_budget_stops_before_opening_connection(env):
    class Expired(Engine):
        def extract(self, *args):
            self.source_deadline = time.monotonic() - 1
            return super().extract(*args)

    result = run(env, engine_cls=Expired)
    assert result["state"] == "FAILED" and "10분 초과" in result["error"]
