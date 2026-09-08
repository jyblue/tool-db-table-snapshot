"""Windows/macOS process identity and recovery, independent of Streamlit."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

from . import db
from .engine import reconcile
from .secrets import password
from .store import ACTIVE


def owned_process(run):
    if not run["pid"] or not run["process_created"]:
        return None
    try:
        p = psutil.Process(run["pid"])
        if abs(p.create_time() - run["process_created"]) > 0.01:
            return None
        command = p.cmdline()
        if "snapshot.worker" not in command or run["id"] not in command:
            return None
        if not p.is_running() or p.status() == psutil.STATUS_ZOMBIE:
            return None
        return p
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def start(store, spec, ephemeral, retry_of=None):
    # Resolve secrets before queueing. Only an anonymous stdin pipe transports them.
    secrets = {i: password(p, ephemeral) for i, p in spec["profiles"].items()}
    run_id = store.queue(spec, retry_of)
    kwargs = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "snapshot.worker", "--root", str(store.root), "--run-id", run_id],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(Path(__file__).resolve().parent.parent),
            **kwargs,
        )
        # The worker atomically claims its own identity; the UI never overwrites it.
        proc.stdin.write(json.dumps(secrets).encode("utf8"))
        proc.stdin.close()
    except BaseException:
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=10)
        store.update_run(run_id, state="FAILED", ended=time.time(), error="worker 실행/비밀 전달 실패")
        raise
    return run_id


def recover_local(store):
    for row in store.runs():
        if row["state"] not in ACTIVE:
            continue
        if owned_process(row):
            continue
        if row["pid"]:
            try:
                # Inability to inspect a process is not evidence that it exited.
                candidate = psutil.Process(row["pid"])
                candidate.cmdline()
            except psutil.AccessDenied:
                continue
            except psutil.NoSuchProcess:
                pass
        if row["pid"] is None and time.time() - row["created"] < 30:
            continue
        if not store.interrupt_if_current(row):
            continue
        for item in store.tables(row["id"]):
            if item["stage"] not in ("PUBLISHED", "FAILED", "CANCELLED"):
                store.update_table(
                    row["id"],
                    item["ordinal"],
                    stage="FAILED",
                    error="worker 중단; 재시도 가능",
                    connection_status="로컬 종료 / 서버 미확인",
                )


def force_stop(store, run_id):
    run = store.run(run_id)
    if run["state"] not in ACTIVE:
        raise ValueError("실행 중인 작업이 아닙니다.")
    process = owned_process(run)
    if not process:
        recover_local(store)
        return
    # Recheck immediately before terminating, including creation time and run ID.
    if not owned_process(store.run(run_id)):
        raise ValueError("프로세스 식별이 변경되었습니다.")
    store.record_activity("FORCE_STOP_REQUESTED", run_id)
    process.kill()
    try:
        process.wait(timeout=5)
    except psutil.TimeoutExpired:
        raise ValueError("종료 요청 후 프로세스 종료를 아직 확인하지 못했습니다.") from None
    recover_local(store)


def retry(store, run_id, ephemeral):
    old = store.run(run_id)
    if old["publish_pending"]:
        reconcile(store, run_id, ephemeral)
        old = store.run(run_id)
    if old["state"] not in ("FAILED", "CANCELLED", "INTERRUPTED"):
        raise ValueError("실패/취소/중단된 실행만 재시도할 수 있습니다.")
    return start(store, old["spec"], ephemeral, retry_of=run_id)


def cleanup(store, run_id, ephemeral):
    run = store.run(run_id)
    if run["state"] in ACTIVE or run["publish_pending"] or owned_process(run):
        raise ValueError("실행 중이거나 공개 결과가 미확인입니다.")
    # Any retry may still own/reference these completed files.
    if any(r["state"] in ACTIVE for r in store.runs()):
        raise ValueError("다른 실행이 완료 파일을 사용 중일 수 있습니다.")
    spec = run["spec"]
    profile = spec["profiles"][spec["jobs"][0]["target_id"]]
    conn = db.connect(
        profile, password(profile, ephemeral), spec["jobs"][0], target=True, select_database=False
    )
    try:
        db.check_target_isolation(
            conn,
            profile,
            [p for p in spec["profiles"].values() if p["role"] == "source"],
            lambda p: password(p, ephemeral),
        )
        conn.select_db(profile["database"])
        with conn.cursor() as cur:
            for item in store.tables(run_id):
                expected = f"_snapshot_s_{run_id}_{item['ordinal']}"
                if item["staging"] == expected:
                    cur.execute(f"DROP TABLE IF EXISTS {db.ident(expected)}")
    finally:
        conn.close()
    directory = Path(spec["work_dir"]) / run_id
    if directory.exists():
        # Retain logs. Only remove engine-owned files in this run's directory.
        for path in directory.iterdir():
            if path.name != "events.jsonl" and path.is_file() and not path.is_symlink():
                path.unlink()
    for item in store.tables(run_id):
        if item["file"] and Path(item["file"]).parent == directory:
            store.update_table(run_id, item["ordinal"], file=None)
    store.record_activity("FILES_CLEANED", run_id)
