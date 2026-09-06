from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import threading
import time
from pathlib import Path

import pymysql

from . import codec, db
from .secrets import password, safe_error


class Cancelled(Exception):
    pass


def check_disk(directory, minimum=16 * 1024 * 1024):
    path = Path(directory).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    import tempfile

    with tempfile.TemporaryFile(dir=path) as f:
        f.write(b"write-check")
        f.flush()
    free = shutil.disk_usage(path).free
    if free < minimum:
        raise OSError("중간 저장 디스크 여유 공간 부족")
    return path, free


def validate_job(job):
    for k in ("name", "source_id", "target_id", "source_table", "target_table"):
        if not job.get(k):
            raise ValueError(f"{k} 값이 필요합니다.")
    db.ident(job["source_table"])
    db.ident(job["target_table"])
    if job["target_table"].casefold().startswith(db.PREFIX):
        raise ValueError("_snapshot_ 접두사는 도구 전용입니다.")
    if job["read_mode"] not in ("pk", "stream"):
        raise ValueError("읽기 모드 오류")
    for k, lo, hi in (
        ("batch_rows", 1, 100000),
        ("wait_ms", 0, 60000),
        ("connect_timeout", 1, 31536000),
        ("read_timeout", 1, 86400),
        ("write_timeout", 1, 86400),
        ("retries", 0, 5),
    ):
        if not isinstance(job[k], int) or not lo <= job[k] <= hi:
            raise ValueError(f"{k}: {lo}~{hi} 범위 정수가 필요합니다.")


def build_spec(store, job_ids, date, limit, work_dir):
    if not job_ids or len(set(job_ids)) != len(job_ids):
        raise ValueError("중복 없이 작업을 선택하세요.")
    dt.date.fromisoformat(str(date))
    if limit is not None and (not isinstance(limit, int) or limit < 1):
        raise ValueError("테스트 행 수는 양수입니다.")
    all_jobs = {j["id"]: j for j in store.jobs()}
    profiles = {p["id"]: p for p in store.profiles()}
    jobs = [all_jobs[i] for i in job_ids]
    for job in jobs:
        validate_job(job)
        if profiles[job["source_id"]]["role"] != "source" or profiles[job["target_id"]]["role"] != "target":
            raise ValueError("Source/Target 역할 불일치")
    if len({j["target_id"] for j in jobs}) != 1:
        raise ValueError("묶음 원자 공개를 위해 같은 Target 연결을 선택하세요.")
    if len({j["target_table"].casefold() for j in jobs}) != len(jobs):
        raise ValueError("대상 테이블 매핑이 중복됩니다.")
    path, _ = check_disk(work_dir)
    return {
        "date": str(date),
        "limit": limit,
        "work_dir": str(path),
        "jobs": jobs,
        "profiles": {i: profiles[i] for j in jobs for i in (j["source_id"], j["target_id"])},
    }


def marker_schema():
    return {
        "columns": [
            {
                "name": "run_id",
                "type": "varchar(32)",
                "nullable": "NO",
                "charset": "ascii",
                "collation": "ascii_bin",
                "extra": "",
            },
            {
                "name": "snapshot_date",
                "type": "date",
                "nullable": "NO",
                "charset": None,
                "collation": None,
                "extra": "",
            },
            {
                "name": "tables_json",
                "type": "longtext",
                "nullable": "NO",
                "charset": "utf8mb4",
                "collation": "utf8mb4_bin",
                "extra": "",
            },
        ],
        "pk": ["run_id"],
        "unique": {"PRIMARY": ["run_id"]},
        "key": ["run_id"],
    }


def acquire_lock(conn, database):
    lock = "snapshot:" + hashlib.sha256(database.encode()).hexdigest()[:48]
    if db.rows(conn, "SELECT GET_LOCK(%s, %s)", (lock, 10))[0][0] != 1:
        raise ValueError("이전 대상 세션의 종료/커밋을 아직 확인할 수 없습니다. 잠시 후 복구하세요.")


def reconcile(store, run_id, secrets):
    run = store.run(run_id)
    if not run["publish_pending"]:
        return run["state"]
    spec = run["spec"]
    profile = spec["profiles"][spec["jobs"][0]["target_id"]]
    conn = db.connect(profile, password(profile, secrets), spec["jobs"][0], target=True)
    try:
        acquire_lock(conn, profile["database"])
        result = db.rows(
            conn, f"SELECT snapshot_date,tables_json FROM {db.ident(db.MARKER)} WHERE run_id=%s", (run_id,)
        )
        if result:
            if str(result[0][0]) != spec["date"] or json.loads(result[0][1]) != [
                j["target_table"] for j in spec["jobs"]
            ]:
                raise ValueError("대상 공개 기록 불일치: 관리자 확인이 필요합니다.")
            state = "SUCCESS"
            for item in store.tables(run_id):
                store.update_table(run_id, item["ordinal"], stage="PUBLISHED", ended=time.time())
        else:
            state = "CANCELLED" if run["cancel"] else "INTERRUPTED"
        store.update_run(
            run_id,
            state=state,
            publish_pending=0,
            ended=time.time(),
            error=None if result else "대상 잠금 획득 후 공개 기록 없음 확인; 기존 성공본 유지",
        )
        return state
    finally:
        conn.close()


class Engine:
    def __init__(self, store, run_id, secrets):
        self.store, self.run_id, self.secrets = store, run_id, secrets
        self.run = store.run(run_id)
        self.spec = self.run["spec"]
        self.stop = threading.Event()
        self.last_check = 0
        self.conn = None
        self.current = None
        self.stage_names = []
        self.done_files = []
        self.pending_progress = {}
        self.last_progress_write = 0
        self.directory = Path(self.spec["work_dir"]) / run_id

    def check(self, force=False):
        if force or time.monotonic() - self.last_check > 0.1:
            self.last_check = time.monotonic()
            if self.store.run(self.run_id)["cancel"]:
                raise Cancelled("사용자가 정상 취소를 요청했습니다.")

    def pause(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.check(force=True)
            self.stop.wait(min(0.1, max(0, end - time.monotonic())))

    def heartbeat(self):
        while not self.stop.wait(1):
            try:
                self.store.update_run(self.run_id, heartbeat=time.time())
            except Exception:
                pass

    def log(self, level, message, table=None):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / "events.jsonl"
        event = {
            "time": dt.datetime.now(dt.UTC).isoformat(),
            "level": level,
            "run_id": self.run_id,
            "table": table,
            "message": message,
        }
        with path.open("a", encoding="utf8") as fp:
            path.chmod(0o600)
            fp.write(json.dumps(event, ensure_ascii=False) + "\n")

    def update(self, **values):
        self.pending_progress.update(values)
        if (
            values.keys() <= {"extracted", "loaded", "bytes", "progress"}
            and time.monotonic() - self.last_progress_write < 0.25
        ):
            return
        self.store.update_table(self.run_id, self.current, **self.pending_progress)
        self.pending_progress.clear()
        self.last_progress_write = time.monotonic()

    def retry(self, action, job):
        for attempt in range(job["retries"] + 1):
            self.check(force=True)
            try:
                return action()
            except (pymysql.OperationalError, pymysql.InterfaceError) as exc:
                code = exc.args[0] if exc.args else None
                if code not in (2002, 2003, 2006, 2013, 0) or attempt == job["retries"]:
                    raise
                self.log("WARNING", f"통신 실패; 처음부터 재시도 {attempt + 1}/{job['retries']}")
                self.pause(min(2**attempt, 10))

    def source(self, job):
        p = self.spec["profiles"][job["source_id"]]
        return db.Source(p, password(p, self.secrets), job)

    def target(self):
        job = self.spec["jobs"][0]
        p = self.spec["profiles"][job["target_id"]]
        return db.connect(p, password(p, self.secrets), job, target=True)

    def extract(self, job, schema, path):
        src = self.source(job)
        self.store.update_run(self.run_id, server_status="Source 읽기 연결됨 / Target 연결됨")
        writer = codec.Writer(path)
        self.update(
            stage="EXTRACTING",
            extracted=0,
            bytes=0,
            read_started=time.time(),
            read_ended=None,
            connection_status="Source 연결됨",
        )
        try:
            # Detect changes since preflight before issuing the data query.
            if db.schema(src.rows, src.database, job["source_table"]) != schema:
                raise ValueError("추출 전 원본 스키마 변경 감지")
            columns, key = schema["columns"], schema["key"]
            if job["read_mode"] == "pk":
                last = None
                positions = [next(i for i, c in enumerate(columns) if c["name"] == k) for k in key]
                while self.spec["limit"] is None or writer.rows < self.spec["limit"]:
                    self.check(force=True)
                    count = job["batch_rows"]
                    if self.spec["limit"] is not None:
                        count = min(count, self.spec["limit"] - writer.rows)
                    # Autocommit SELECT fully consumed/closed before waiting.
                    with src.read(job["source_table"], columns, key, last, count) as cur:
                        received = 0
                        while True:
                            self.check()
                            chunk = cur.fetchmany(min(count, 256))
                            if not chunk:
                                break
                            writer.write(chunk)
                            received += len(chunk)
                            last = tuple(chunk[-1][i] for i in positions)
                            self.update(extracted=writer.rows, bytes=writer.size, progress=time.time())
                    if received < count or (self.spec["limit"] and writer.rows >= self.spec["limit"]):
                        break
                    check_disk(self.directory)
                    if job["wait_ms"]:
                        self.update(stage="SLEEPING")
                        self.pause(job["wait_ms"] / 1000)
                        self.update(stage="EXTRACTING")
            else:
                with src.read(job["source_table"], columns, key, limit=self.spec["limit"]) as cur:
                    try:
                        while True:
                            self.check(force=True)
                            chunk = cur.fetchmany(min(job["batch_rows"], 256))
                            if not chunk:
                                break
                            writer.write(chunk)
                            self.update(extracted=writer.rows, bytes=writer.size, progress=time.time())
                            check_disk(self.directory)
                    except BaseException:
                        src.close()
                        raise
            self.check(force=True)
            if db.schema(src.rows, src.database, job["source_table"]) != schema:
                raise ValueError("추출 도중 원본 스키마 변경 감지")
            result = writer.complete(self.file_metadata(job, schema, self.run_id))
            self.update(file=str(path), extracted=writer.rows, bytes=writer.size)
            return result
        except BaseException:
            src.close()
            raise
        finally:
            writer.close()
            src.close()
            self.update(connection_status="Source 닫힘", read_ended=time.time())
            self.store.update_run(self.run_id, server_status="Source 닫힘 / Target 연결됨")

    def file_metadata(self, job, schema, run_id):
        return {
            "run_id": run_id,
            "source_id": job["source_id"],
            "table": job["source_table"],
            "schema": schema,
            "date": self.spec["date"],
            "limit": self.spec["limit"],
        }

    def load(self, job, expected, path, manifest, stage):
        if self.conn and self.conn.open:
            self.conn.close()
        self.conn = self.target()
        self.update(stage="LOADING", loaded=0, connection_status="Target 연결됨")
        with self.conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {db.ident(stage)}")
            cur.execute(db.create_sql(stage, expected))
        cols = ",".join(db.ident(c["name"]) for c in expected["columns"])
        sql = (
            f"INSERT INTO {db.ident(stage)} ({cols}) VALUES ("
            + ",".join(["%s"] * len(expected["columns"]))
            + ")"
        )
        loaded = 0
        for chunk in codec.batches(path, min(job["batch_rows"], 1000)):
            self.check(force=True)
            self.conn.begin()
            try:
                with self.conn.cursor(db.StrictCursor) as cur:
                    # A bounded batch; PyMySQL emits multi-row INSERT statements.
                    cur.max_stmt_length = 1024 * 1024
                    cur.executemany(
                        sql,
                        [(*map(db.bind_value, r), dt.date.fromisoformat(self.spec["date"])) for r in chunk],
                    )
                    if cur.warning_count:
                        raise ValueError("적재 경고 감지: 타입 변환/잘림을 확인하세요.")
                self.conn.commit()
            except BaseException:
                self.conn.rollback()
                raise
            loaded += len(chunk)
            self.update(loaded=loaded, progress=time.time())
        self.update(stage="VALIDATING")
        actual = db.rows(self.conn, f"SELECT COUNT(*) FROM {db.ident(stage)}")[0][0]
        if actual != manifest["rows"] or loaded != manifest["rows"]:
            raise ValueError("추출/적재 행 수 불일치")
        self.update(stage="READY", ended=time.time())

    def execute(self):
        thread = threading.Thread(target=self.heartbeat, daemon=True)
        thread.start()
        try:
            self.check(force=True)
            check_disk(self.directory)
            self.log("INFO", "실행 시작 · UTC 시간 값 보존 · 원본 테이블 순차 읽기")
            self.conn = self.retry(self.target, self.spec["jobs"][0])
            self.store.update_run(self.run_id, server_status="Target 연결됨 / Source 사전 검사")
            target_profile = self.spec["profiles"][self.spec["jobs"][0]["target_id"]]
            target_identity = db.identity(lambda sql: db.rows(self.conn, sql))
            schemas = []
            # Check every source against target before ANY persistent target DDL/DML.
            for job in self.spec["jobs"]:
                self.check(force=True)
                src = self.retry(lambda: self.source(job), job)
                try:
                    db.assert_distinct(
                        self.spec["profiles"][job["source_id"]],
                        target_profile,
                        db.identity(src.rows),
                        target_identity,
                    )
                    sch = db.schema(src.rows, src.database, job["source_table"])
                    exp = db.expected_schema(sch)
                    if job["read_mode"] == "pk" and not sch["key"]:
                        raise ValueError(
                            f"{job['source_table']}: 적합한 키가 없어 단일 스트리밍 모드를 선택해야 합니다."
                        )
                    schemas.append((sch, exp))
                finally:
                    src.close()
            db.prepare(self.conn, target_profile["database"], db.MARKER, marker_schema())
            for job, (_, expected) in zip(self.spec["jobs"], schemas):
                if self.spec["limit"] is None:
                    db.prepare(self.conn, target_profile["database"], job["target_table"], expected)
                else:
                    # Test schema/index feasibility without preparing a formal table.
                    for c in expected["columns"]:
                        db.column_ddl(c)
            previous = self.store.tables(self.run["retry_of"]) if self.run["retry_of"] else []
            for n, (job, (sch, expected)) in enumerate(zip(self.spec["jobs"], schemas)):
                self.current = n
                self.check(force=True)
                stage = f"_snapshot_s_{self.run_id}_{n}"
                self.stage_names.append(stage)
                self.update(started=time.time(), staging=stage)
                path = self.directory / f"{n}.jsonl"
                reused = False
                if previous and previous[n]["file"]:
                    old_path = Path(previous[n]["file"])
                    if old_path.exists():
                        # Manifest's owner can be an earlier retry ancestor.
                        owner = old_path.parent.name
                        manifest = codec.verify(old_path, self.file_metadata(job, sch, owner), self.check)
                        path = old_path
                        self.update(
                            file=str(path),
                            extracted=manifest["rows"],
                            bytes=manifest["bytes"],
                            read_started=previous[n]["read_started"],
                            read_ended=previous[n]["read_ended"],
                        )
                        self.log("INFO", "검증된 완료 파일 재사용", job["source_table"])
                        reused = True
                if not reused:
                    manifest = self.retry(lambda: self.extract(job, sch, path), job)
                codec.verify(path, self.file_metadata(job, sch, path.parent.name), self.check)
                self.done_files.append(path)
                self.retry(lambda: self.load(job, expected, path, manifest, stage), job)
                self.log(
                    "INFO",
                    f"검증 완료: {manifest['rows']}행 (행 수 검증; 전체 값 동일성 보장은 아님)",
                    job["source_table"],
                )
            self.check(force=True)
            if self.spec["limit"] is None:
                if not self.store.begin_publish(self.run_id):
                    raise Cancelled("공개 전 취소")
                acquire_lock(self.conn, target_profile["database"])
                # Repeat schema checks after extraction; DDL remains outside publication.
                for job, (_, expected) in zip(self.spec["jobs"], schemas):
                    db.prepare(self.conn, target_profile["database"], job["target_table"], expected)
                self.conn.begin()
                try:
                    for job, stage in zip(self.spec["jobs"], self.stage_names):
                        self.check(force=True)
                        with self.conn.cursor() as cur:
                            cur.execute(
                                f"DELETE FROM {db.ident(job['target_table'])} WHERE snapshot_date=%s",
                                (self.spec["date"],),
                            )
                            cur.execute(
                                f"INSERT INTO {db.ident(job['target_table'])} SELECT * FROM {db.ident(stage)}"
                            )
                            if cur.warning_count:
                                raise ValueError("공개 적재 경고 감지")
                    self.check(force=True)
                    with self.conn.cursor() as cur:
                        cur.execute(
                            f"INSERT INTO {db.ident(db.MARKER)} VALUES (%s,%s,%s)",
                            (
                                self.run_id,
                                self.spec["date"],
                                json.dumps([j["target_table"] for j in self.spec["jobs"]]),
                            ),
                        )
                    self.conn.commit()
                except BaseException:
                    try:
                        self.conn.rollback()
                    except Exception:
                        pass
                    raise
                for n in range(len(self.spec["jobs"])):
                    self.store.update_table(self.run_id, n, stage="PUBLISHED", ended=time.time())
            self.store.update_run(self.run_id, state="SUCCESS", publish_pending=0, ended=time.time())
            self.log(
                "INFO",
                "전체 공개 커밋 완료"
                if self.spec["limit"] is None
                else "테스트 성공 · 정식 스냅샷 변경 없음",
            )
            for path in self.done_files:
                path.unlink(missing_ok=True)
                path.with_suffix(".manifest.json").unlink(missing_ok=True)
        except BaseException as exc:
            message = safe_error(exc, self.secrets.values())
            state = "CANCELLED" if isinstance(exc, Cancelled) else "FAILED"
            if self.conn:
                try:
                    self.conn.close()
                except Exception:
                    pass
                self.conn = None
            if self.store.run(self.run_id)["publish_pending"]:
                try:
                    state = reconcile(self.store, self.run_id, self.secrets)
                except Exception:
                    state = "INTERRUPTED"
                    message += " · 공개 결과 미확인; 대상 기록 복구 필요"
            # Cleanup failure after committed success must not overwrite durable success.
            if self.store.run(self.run_id)["state"] == "SUCCESS":
                state = "SUCCESS"
            if self.current is not None and state != "SUCCESS":
                self.update(
                    stage="CANCELLED" if state == "CANCELLED" else "FAILED", error=message, ended=time.time()
                )
            self.store.update_run(
                self.run_id, state=state, error=message if state != "SUCCESS" else None, ended=time.time()
            )
            for item in self.store.tables(self.run_id):
                if item["stage"] == "WAITING":
                    self.store.update_table(
                        self.run_id, item["ordinal"], stage="CANCELLED", error="후속 작업 실행 중단"
                    )
            try:
                self.log("ERROR", message)
            except OSError:
                pass
        finally:
            if self.conn:
                try:
                    self.conn.close()
                except Exception:
                    pass
            # Staging cleanup can be retried explicitly after forced termination.
            try:
                conn = self.target()
                try:
                    if not self.store.run(self.run_id)["publish_pending"]:
                        with conn.cursor() as cur:
                            for stage in self.stage_names:
                                cur.execute(f"DROP TABLE IF EXISTS {db.ident(stage)}")
                finally:
                    conn.close()
            except Exception:
                pass
            self.store.update_run(self.run_id, server_status="로컬 연결 닫힘 / 서버 종료 미확인")
            for item in self.store.tables(self.run_id):
                self.store.update_table(self.run_id, item["ordinal"], connection_status="로컬 연결 닫힘")
            self.stop.set()
            thread.join(timeout=2)
