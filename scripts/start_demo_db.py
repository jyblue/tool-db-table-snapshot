"""Start the local Docker test DB and add repeatable synthetic sample data."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "-f", str(ROOT / "compose.test.yml")]


def main():
    try:
        subprocess.run([*COMPOSE, "up", "-d", "--wait"], cwd=ROOT, check=True)
        sql = Path(__file__).with_name("demo_data.sql").read_text(encoding="utf-8")
        for service, statements in (
            ("mariadb", sql),
            ("mariadb-target", "CREATE DATABASE IF NOT EXISTS snapshot_target CHARACTER SET utf8mb4;"),
        ):
            subprocess.run(
                [
                    *COMPOSE,
                    "exec",
                    "-T",
                    service,
                    "sh",
                    "-c",
                    'MYSQL_PWD="$MARIADB_ROOT_PASSWORD" exec mariadb -u root --default-character-set=utf8mb4',
                ],
                cwd=ROOT,
                input=statements.encode("utf-8"),
                check=True,
            )
    except FileNotFoundError:
        print("Docker Desktop을 설치하고 실행한 뒤 다시 시도하세요.", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError:
        print(
            "준비 실패: 위 오류와 Docker Desktop 실행 여부, 포트 33316/33318 사용 여부를 확인하세요.",
            file=sys.stderr,
        )
        return 1
    print("\n준비 완료: Source=127.0.0.1:33316 / Target=127.0.0.1:33318 / root / snapshot-test-only")
    print("앱 DB명: Source=snapshot_source, Target=snapshot_target")
    print("원본 테이블: demo_customers, demo_orders, demo_order_items (모두 PK 배치 읽기 지원)")
    print("기존 행과 대상 데이터는 유지됩니다. 테스트 전용이며 운영 환경에 사용하지 마세요.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
