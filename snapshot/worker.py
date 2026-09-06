import argparse
import json
import os
import sys
import time

import psutil

from .engine import Engine
from .secrets import safe_error
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="Independent MariaDB snapshot worker")
    parser.add_argument("--root", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    store = Store(args.root)
    secrets = json.load(sys.stdin)
    with store.db() as con:
        con.execute("BEGIN IMMEDIATE")
        result = con.execute(
            "UPDATE run SET pid=?,process_created=?,started=?,heartbeat=?,state=CASE WHEN cancel=1 THEN 'CANCEL_REQUESTED' ELSE 'RUNNING' END WHERE id=? AND pid IS NULL AND state IN ('QUEUED','CANCEL_REQUESTED')",
            (os.getpid(), psutil.Process().create_time(), time.time(), time.time(), args.run_id),
        )
        if result.rowcount != 1:
            return 2
        store._event(con, "WORKER_STARTED", actor="system", run_id=args.run_id)
    try:
        Engine(store, args.run_id, secrets).execute()
    except BaseException as exc:
        store.update_run(
            args.run_id, state="INTERRUPTED", ended=time.time(), error=safe_error(exc, secrets.values())
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
