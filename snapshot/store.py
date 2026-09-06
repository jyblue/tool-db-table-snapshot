from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

ACTIVE = ("QUEUED", "RUNNING", "PUBLISHING", "CANCEL_REQUESTED")
TERMINAL = ("SUCCESS", "FAILED", "CANCELLED", "INTERRUPTED")


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "state.sqlite3"
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS connection_profile(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS backup_job(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS run(
                    id TEXT PRIMARY KEY, state TEXT NOT NULL, spec TEXT NOT NULL,
                    created REAL NOT NULL, started REAL, ended REAL, pid INTEGER, process_created REAL,
                    heartbeat REAL, cancel INTEGER DEFAULT 0, error TEXT, retry_of TEXT,
                    publish_pending INTEGER DEFAULT 0, server_status TEXT DEFAULT '미연결');
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON run((1))
                    WHERE state IN ('QUEUED','RUNNING','PUBLISHING','CANCEL_REQUESTED');
                CREATE TABLE IF NOT EXISTS run_table(
                    run_id TEXT NOT NULL, ordinal INTEGER NOT NULL, job_id TEXT NOT NULL,
                    stage TEXT DEFAULT 'WAITING', extracted INTEGER DEFAULT 0, loaded INTEGER DEFAULT 0,
                    bytes INTEGER DEFAULT 0, progress REAL, started REAL, ended REAL,
                    read_started REAL, read_ended REAL,
                    file TEXT, staging TEXT, error TEXT, connection_status TEXT DEFAULT '대기',
                    PRIMARY KEY(run_id, ordinal));
            """)
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def profiles(self):
        return self._list("connection_profile")

    def jobs(self):
        return self._list("backup_job")

    def _list(self, table):
        with self.db() as db:
            return [json.loads(r[0]) for r in db.execute(f"SELECT data FROM {table} ORDER BY rowid")]

    def save(self, table, data):
        if table not in ("connection_profile", "backup_job"):
            raise ValueError("Invalid config table")
        data = dict(data)
        if any("password" in k.lower() for k in data):
            raise ValueError("비밀번호는 설정에 저장할 수 없습니다.")
        data.setdefault("id", uuid.uuid4().hex)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_idle(db)
            db.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?)", (data["id"], json.dumps(data)))
        return data["id"]

    def delete(self, table, item_id):
        if table not in ("connection_profile", "backup_job"):
            raise ValueError("Invalid config table")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_idle(db)
            if table == "connection_profile":
                for row in db.execute("SELECT data FROM backup_job"):
                    job = json.loads(row[0])
                    if item_id in (job["source_id"], job["target_id"]):
                        raise ValueError("연결을 사용하는 작업을 먼저 삭제하세요.")
            db.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))

    def _assert_idle(self, db):
        if db.execute(
            "SELECT 1 FROM run WHERE state IN ('QUEUED','RUNNING','PUBLISHING','CANCEL_REQUESTED') OR publish_pending=1"
        ).fetchone():
            raise ValueError("실행 중이거나 공개 결과 확인이 필요합니다. 먼저 복구하세요.")

    def queue(self, spec, retry_of=None):
        run_id = uuid.uuid4().hex
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            self._assert_idle(db)
            db.execute(
                "INSERT INTO run(id,state,spec,created,retry_of) VALUES (?,?,?,?,?)",
                (run_id, "QUEUED", json.dumps(spec), time.time(), retry_of),
            )
            db.executemany(
                "INSERT INTO run_table(run_id,ordinal,job_id) VALUES (?,?,?)",
                [(run_id, n, j["id"]) for n, j in enumerate(spec["jobs"])],
            )
        return run_id

    def runs(self):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM run ORDER BY created DESC LIMIT 100")]

    def run(self, run_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM run WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise ValueError("실행을 찾을 수 없습니다.")
            r = dict(row)
            r["spec"] = json.loads(r["spec"])
            return r

    def tables(self, run_id):
        with self.db() as db:
            return [
                dict(r)
                for r in db.execute("SELECT * FROM run_table WHERE run_id=? ORDER BY ordinal", (run_id,))
            ]

    def update_run(self, run_id, **values):
        allowed = {
            "state",
            "started",
            "ended",
            "pid",
            "process_created",
            "heartbeat",
            "cancel",
            "error",
            "publish_pending",
            "server_status",
        }
        if not values.keys() <= allowed:
            raise ValueError("Invalid run fields")
        with self.db() as db:
            db.execute(
                "UPDATE run SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",
                (*values.values(), run_id),
            )

    def update_table(self, run_id, ordinal, **values):
        allowed = {
            "stage",
            "extracted",
            "loaded",
            "bytes",
            "progress",
            "started",
            "ended",
            "file",
            "staging",
            "error",
            "connection_status",
            "read_started",
            "read_ended",
        }
        if not values.keys() <= allowed:
            raise ValueError("Invalid table fields")
        with self.db() as db:
            db.execute(
                "UPDATE run_table SET "
                + ",".join(f"{k}=?" for k in values)
                + " WHERE run_id=? AND ordinal=?",
                (*values.values(), run_id, ordinal),
            )

    def cancel(self, run_id):
        with self.db() as db:
            db.execute(
                "UPDATE run SET cancel=1,state='CANCEL_REQUESTED' WHERE id=? AND state IN ('QUEUED','RUNNING','PUBLISHING')",
                (run_id,),
            )

    def begin_publish(self, run_id):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT cancel FROM run WHERE id=?", (run_id,)).fetchone()
            if row[0]:
                return False
            db.execute("UPDATE run SET state='PUBLISHING',publish_pending=1 WHERE id=?", (run_id,))
            return True

    def interrupt_if_current(self, observed):
        with self.db() as db:
            result = db.execute(
                "UPDATE run SET state='INTERRUPTED',ended=?,error=?,server_status=? "
                "WHERE id=? AND pid IS ? AND process_created IS ? "
                "AND state IN ('QUEUED','RUNNING','PUBLISHING','CANCEL_REQUESTED')",
                (
                    time.time(),
                    "worker 종료 감지; 완료 파일 보관",
                    "로컬 종료 완료 / 서버 종료 미확인",
                    observed["id"],
                    observed["pid"],
                    observed["process_created"],
                ),
            )
            return result.rowcount == 1
