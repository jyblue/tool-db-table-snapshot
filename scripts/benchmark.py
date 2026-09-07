"""Opt-in benchmark for the disposable MariaDB on localhost, never production."""

import argparse
import json
import sys
import tempfile
import time
import uuid
from pathlib import Path

import psutil
import pymysql

from snapshot import process
from snapshot.engine import build_spec
from snapshot.store import Store


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=33316)
    parser.add_argument("--target-port", type=int, default=33318)
    parser.add_argument("--tables", type=int, default=1)
    parser.add_argument("--mib-per-table", type=int, default=16)
    parser.add_argument("--output", default="benchmark-result.json")
    parser.add_argument(
        "--reset-disposable-benchmark-dbs",
        action="store_true",
        required=True,
        help="실험용 서버에 고유한 벤치마크 DB를 생성한다는 명시적 확인",
    )
    args = parser.parse_args()
    if not 1 <= args.tables <= 20 or not 1 <= args.mib_per_table <= 10240:
        parser.error("tables: 1..20; mib-per-table: 1..10240")
    root = pymysql.connect(
        host="127.0.0.1", port=args.port, user="root", password="snapshot-test-only", autocommit=True
    )
    target_root = pymysql.connect(
        host="127.0.0.1", port=args.target_port, user="root", password="snapshot-test-only", autocommit=True
    )
    if args.port == args.target_port:
        parser.error("Source and Target must use separate MariaDB instances")
    with root.cursor() as cur:
        cur.execute("SELECT @@hostname, @@port, @@datadir")
        source_identity = cur.fetchone()
    with target_root.cursor() as cur:
        cur.execute("SELECT @@hostname, @@port, @@datadir")
        target_identity = cur.fetchone()
    if source_identity == target_identity:
        root.close()
        target_root.close()
        parser.error("Source and Target must be separate MariaDB instances")
    suffix = uuid.uuid4().hex[:10]
    source_db = f"snapshot_bench_source_{suffix}"
    target_db = f"snapshot_bench_target_{suffix}"
    # Use fresh names so a misconfigured benchmark cannot remove an existing schema.
    for conn, name in ((root, source_db), (target_root, target_db)):
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE `{name}`")
    with root.cursor() as cur:
        cur.execute("CREATE USER IF NOT EXISTS 'snapshot_bench_reader'@'%' IDENTIFIED BY 'bench-read-only'")
        cur.execute(f"GRANT SELECT ON `{source_db}`.* TO 'snapshot_bench_reader'@'%'")
        for i in range(args.tables):
            cur.execute(
                f"CREATE TABLE `{source_db}`.t{i} (id INT PRIMARY KEY, payload LONGBLOB NOT NULL) ENGINE=InnoDB"
            )
            for row in range(args.mib_per_table * 4):
                cur.execute(
                    f"INSERT INTO `{source_db}`.t{i} VALUES (%s,REPEAT(%s,262144))", (row, "x")
                )
    with target_root.cursor() as cur:
        cur.execute("SHOW GLOBAL STATUS LIKE 'Innodb_os_log_written'")
        redo_before = int(cur.fetchone()[1])
    state_dir = Path(tempfile.mkdtemp(prefix="snapshot-benchmark-"))
    store = Store(state_dir)
    source = dict(
        id="s",
        name="Benchmark source",
        role="source",
        host="127.0.0.1",
        port=args.port,
        database=source_db,
        user="snapshot_bench_reader",
        tls=False,
    )
    target = dict(
        source,
        id="t",
        name="Benchmark target",
        role="target",
        database=target_db,
        user="root",
        port=args.target_port,
    )
    for p in (source, target):
        store.save("connection_profile", p)
    ids = []
    for i in range(args.tables):
        ids.append(
            store.save(
                "backup_job",
                dict(
                    id=str(i),
                    name=f"Table {i}",
                    source_id="s",
                    target_id="t",
                    source_table=f"t{i}",
                    target_table=f"t{i}",
                    read_mode="pk",
                    batch_rows=16,
                    wait_ms=300,
                    connect_timeout=10,
                    read_timeout=120,
                    write_timeout=120,
                    retries=0,
                ),
            )
        )
    spec = build_spec(store, ids, "2026-09-06", None, state_dir / "files")
    start = time.monotonic()
    rid = process.start(store, spec, {"s": "bench-read-only", "t": "snapshot-test-only"})
    peak_rss = peak_files = 0
    publish_start = publish_end = None
    next_report = 0
    while True:
        run = store.run(rid)
        if run["pid"]:
            try:
                peak_rss = max(peak_rss, psutil.Process(run["pid"]).memory_info().rss)
            except psutil.NoSuchProcess:
                pass
        if run["state"] == "PUBLISHING" and publish_start is None:
            publish_start = time.monotonic()
        if run["state"] in ("SUCCESS", "FAILED", "CANCELLED", "INTERRUPTED"):
            publish_end = time.monotonic()
            break
        peak_files = max(
            peak_files, sum(p.stat().st_size for p in (state_dir / "files").rglob("*") if p.is_file())
        )
        if time.monotonic() > next_report:
            print(
                f"{run['state']} elapsed={time.monotonic() - start:.1f}s peak_RSS={peak_rss / 1024**2:.1f}MiB",
                flush=True,
            )
            next_report = time.monotonic() + 10
        time.sleep(0.1)
    with target_root.cursor() as cur:
        cur.execute("SHOW GLOBAL STATUS LIKE 'Innodb_os_log_written'")
        redo_after = int(cur.fetchone()[1])
        for i in range(args.tables):
            cur.execute(f"ANALYZE TABLE `{target_db}`.t{i}")
            cur.fetchall()
        cur.execute(
            "SELECT SUM(DATA_LENGTH+INDEX_LENGTH) FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s",
            (target_db,),
        )
        target_bytes = int(cur.fetchone()[0] or 0)
    result = dict(
        state=run["state"],
        error=run["error"],
        tables=args.tables,
        mib_per_table=args.mib_per_table,
        elapsed_seconds=time.monotonic() - start,
        peak_worker_rss_bytes=peak_rss,
        peak_local_file_bytes=peak_files,
        target_redo_bytes_delta=redo_after - redo_before,
        target_table_bytes_estimate=target_bytes,
        publication_seconds_sampled=publish_end - publish_start if publish_start else None,
        extracted_rows=sum(t["extracted"] for t in store.tables(rid)),
        state_directory=str(state_dir),
        notes="Synthetic repeated 256KiB BLOB rows; 100ms samples; excludes seeding; redo includes staging and publication.",
    )
    Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf8")
    print(json.dumps(result, indent=2), flush=True)
    root.close()
    target_root.close()
    return 0 if run["state"] == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
