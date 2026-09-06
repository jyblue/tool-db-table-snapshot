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
                CREATE INDEX IF NOT EXISTS run_created ON run(created DESC, id DESC);
                CREATE INDEX IF NOT EXISTS run_table_job ON run_table(job_id,run_id);
                CREATE TABLE IF NOT EXISTS activity_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, occurred REAL NOT NULL,
                    actor TEXT NOT NULL, action TEXT NOT NULL, entity_id TEXT,
                    name TEXT, run_id TEXT, detail TEXT);
                CREATE INDEX IF NOT EXISTS activity_run ON activity_log(run_id,id DESC);
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
            existed = db.execute(f"SELECT 1 FROM {table} WHERE id=?", (data["id"],)).fetchone()
            db.execute(f"INSERT OR REPLACE INTO {table} VALUES (?,?)", (data["id"], json.dumps(data)))
            if table == "backup_job":
                self._event(
                    db,
                    "JOB_UPDATED" if existed else "JOB_CREATED",
                    entity_id=data["id"],
                    name=data.get("name"),
                )
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
            old = db.execute(f"SELECT data FROM {table} WHERE id=?", (item_id,)).fetchone()
            db.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
            if table == "backup_job" and old:
                self._event(db, "JOB_DELETED", entity_id=item_id, name=json.loads(old[0]).get("name"))

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
            self._event(
                db,
                "RETRY_REQUESTED" if retry_of else "RUN_REQUESTED",
                run_id=run_id,
                detail=retry_of,
                name=" → ".join(j.get("name", j["id"]) for j in spec["jobs"]),
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
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT state FROM run WHERE id=?", (run_id,)).fetchone()
            db.execute(
                "UPDATE run SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",
                (*values.values(), run_id),
            )
            if old and "state" in values and old[0] != values["state"]:
                self._event(
                    db, "STATE_CHANGED", actor="system", run_id=run_id, detail=f"{old[0]} → {values['state']}"
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
            result = db.execute(
                "UPDATE run SET cancel=1,state='CANCEL_REQUESTED' WHERE id=? AND state IN ('QUEUED','RUNNING','PUBLISHING')",
                (run_id,),
            )
            if result.rowcount:
                self._event(db, "CANCEL_REQUESTED", run_id=run_id)

    def begin_publish(self, run_id):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT cancel FROM run WHERE id=?", (run_id,)).fetchone()
            if row[0]:
                return False
            db.execute("UPDATE run SET state='PUBLISHING',publish_pending=1 WHERE id=?", (run_id,))
            self._event(db, "STATE_CHANGED", actor="system", run_id=run_id, detail="RUNNING → PUBLISHING")
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
            if result.rowcount:
                self._event(db, "PROCESS_INTERRUPTED", actor="system", run_id=observed["id"])
            return result.rowcount == 1

    @staticmethod
    def _event(db, action, *, actor="user", entity_id=None, name=None, run_id=None, detail=None):
        db.execute(
            "INSERT INTO activity_log(occurred,actor,action,entity_id,name,run_id,detail) VALUES (?,?,?,?,?,?,?)",
            (time.time(), actor, action, entity_id, name, run_id, detail),
        )

    def record_activity(self, action, run_id):
        if action not in ("FORCE_STOP_REQUESTED", "FILES_CLEANED"):
            raise ValueError("지원하지 않는 기록 유형")
        with self.db() as db:
            self._event(db, action, run_id=run_id)

    def activities(self, *, run_id=None, limit=50, offset=0):
        where = " WHERE run_id=?" if run_id else ""
        args = [run_id] if run_id else []
        with self.db() as db:
            total = db.execute("SELECT COUNT(*) FROM activity_log" + where, args).fetchone()[0]
            records = db.execute(
                "SELECT * FROM activity_log" + where + " ORDER BY id DESC LIMIT ? OFFSET ?",
                [*args, max(1, min(limit, 200)), max(0, offset)],
            )
            return total, [dict(r) for r in records]

    def history(
        self, *, keyword="", job_id=None, state=None, mode=None, after=None, before=None, limit=50, offset=0
    ):
        clauses, args = [], []
        if keyword.strip():
            clauses.append(
                "(instr(lower(r.id),lower(?))>0 OR EXISTS (SELECT 1 FROM json_each(r.spec,'$.jobs') j "
                "WHERE instr(lower(coalesce(json_extract(j.value,'$.name'),'')),lower(?))>0 "
                "OR instr(lower(coalesce(json_extract(j.value,'$.source_table'),'')),lower(?))>0 "
                "OR instr(lower(coalesce(json_extract(j.value,'$.target_table'),'')),lower(?))>0))"
            )
            args.extend([keyword.strip()] * 4)
        if job_id:
            clauses.append("EXISTS (SELECT 1 FROM run_table t WHERE t.run_id=r.id AND t.job_id=?)")
            args.append(job_id)
        if state:
            clauses.append("r.state=?")
            args.append(state)
        if mode in ("test", "full"):
            clauses.append("json_extract(r.spec,'$.limit') IS " + ("NOT NULL" if mode == "test" else "NULL"))
        if after is not None:
            clauses.append("r.created>=?")
            args.append(after)
        if before is not None:
            clauses.append("r.created<?")
            args.append(before)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.db() as db:
            total = db.execute("SELECT COUNT(*) FROM run r" + where, args).fetchone()[0]
            records = db.execute(
                "SELECT r.*, (SELECT COUNT(*) FROM run_table t WHERE t.run_id=r.id) AS table_count,"
                "(SELECT COALESCE(SUM(extracted),0) FROM run_table t WHERE t.run_id=r.id) AS extracted,"
                "(SELECT COALESCE(SUM(loaded),0) FROM run_table t WHERE t.run_id=r.id) AS loaded "
                "FROM run r" + where + " ORDER BY r.created DESC,r.id DESC LIMIT ? OFFSET ?",
                [*args, max(1, min(limit, 200)), max(0, offset)],
            )
            return total, [dict(r) for r in records]

    def job_overview(self):
        # Includes deleted jobs through the immutable settings attached to past runs.
        with self.db() as db:
            latest = db.execute("""WITH ranked AS (
                SELECT t.job_id, r.id, r.state, r.created, r.spec,
                  COUNT(*) OVER (PARTITION BY t.job_id) AS execution_count,
                  ROW_NUMBER() OVER (PARTITION BY t.job_id ORDER BY r.created DESC,r.id DESC) AS position
                FROM run_table t JOIN run r ON r.id=t.run_id)
                SELECT * FROM ranked WHERE position=1""")
            result = {}
            for row in latest:
                spec = json.loads(row["spec"])
                job = next(j for j in spec["jobs"] if j["id"] == row["job_id"])
                result[row["job_id"]] = dict(
                    job=job,
                    profiles=spec.get("profiles", {}),
                    count=row["execution_count"],
                    last_run=row["id"],
                    last_state=row["state"],
                    last_created=row["created"],
                    deleted=True,
                )
            current_profiles = {p["id"]: p for p in self.profiles()}
            for job in self.jobs():
                item = result.setdefault(
                    job["id"], dict(count=0, last_run=None, last_state=None, last_created=None)
                )
                item.update(job=job, profiles=current_profiles, deleted=False)
            return sorted(result.values(), key=lambda item: (item["deleted"], item["job"].get("name", "")))
