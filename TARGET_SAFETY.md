# 대상 DB 안전성 점검

검증일: 2026-09-06. MariaDB 11.4, macOS / Python 3.12. 운영 DB와 사용자의 데모 DB(33316)는 변경하지 않고 별도 Docker DB(33317)에서 검증했습니다. 이 문서는 v0.1 시점의 대상 점검 기록입니다. 이후 v0.2 원본 보호 변경으로 키 없는 원본 전체 읽기와 동일 인스턴스 Source/Target은 차단됩니다. 대상 사전 검사의 비고유 인덱스 누락과 최종 반영 부하 제한은 별도 보강 항목입니다.

## 확인한 위험과 방어 범위

| 항목 | 현재 동작 / 영향 | 검증 결과 및 보강 방향 |
| --- | --- | --- |
| 기존 대상의 날짜 인덱스 누락 | PK가 없는 대상의 비고유 인덱스를 검증하지 않음. 날짜 교체 DELETE가 전체 스캔할 수 있음 | 날짜 인덱스를 제거해도 대상 사전 검사 통과, EXPLAIN DELETE에서 `ALL`, 사용 인덱스 없음 재현. 날짜 선두 인덱스 검사 필요 |
| 최종 반영의 큰 트랜잭션 | 선택한 테이블마다 해당 날짜 DELETE와 전체 INSERT SELECT, 마지막에 한 번 COMMIT | 1,205행 적재는 1,000+205행이나 최종 INSERT는 한 문장임을 확인. 총 반영 행·바이트 상한과 실행 전 규모 안내 필요 |
| 대상 적재 속도 | 최대 1,000행씩 커밋하지만 배치 간 대기 없음. `wait_ms`는 원본 PK 조회에만 적용 | 행 배치 상한 확인. 대상 전용 대기/속도 제한 필요. 1MiB INSERT 분할 설정도 단일 대형 행의 크기 상한은 아님 |
| 잠금 충돌 | 행 잠금·DDL 메타데이터 잠금 대기 시간은 DB 기본값에 의존. 취소는 실행 중인 SQL을 즉시 중단하지 않음 | 두 번째 대상 테이블의 행을 다른 연결에서 잠그고 테스트 전용 1초 제한 적용. 오류 1205 후 첫 테이블 변경까지 롤백, 두 테이블 모두 기존 1,205행과 성공 마커 보존. 실제 장기 DDL 대기는 이번에 미검증 |
| 디스크·로그 증가 | 중간 파일 외에도 staging, 정식 테이블, redo/undo/binlog 공간 사용. 대상 DB 여유 공간·복제 지연을 감시하지 않음 | 기존 합성 500MiB 시험에서 redo 약 1,005MiB 증가. 이번에는 대용량 성능 시험을 재실행하지 않음. 대상 용량 경보·보관 정책·실제 환경 부하 시험 필요 |
| 재시도·여러 앱 실행 | 적재 통신 실패 시 해당 staging을 처음부터 다시 적재. 단일 실행 제한은 로컬 저장소 기준이며 DB 잠금은 최종 반영 구간에 적용 | 기존 적재 재시도/공개 복구 시험 통과. 여러 설치가 같은 DB에 동시에 적재하는 부하는 별도 검증 필요 |

큰 DELETE는 undo와 I/O를 늘리며, 행 잠금과 메타데이터 잠금은 서로 다른 시간 제한을 사용합니다. 연결의 `read_timeout`은 서버 쿼리 실행 시간이나 롤백 완료 시간을 보장하는 제한이 아닙니다. 참고: [MariaDB Big DELETEs](https://mariadb.com/docs/server/ha-and-performance/optimization-and-tuning/query-optimizations/big-deletes), [InnoDB 잠금 대기](https://mariadb.com/docs/server/server-usage/storage-engines/innodb/innodb-system-variables), [메타데이터 잠금](https://mariadb.com/docs/server/reference/sql-statements/transactions/metadata-locking).

## 이번 테스트

`tests/test_integration.py`에 다음 3건을 추가했습니다.

- `test_target_row_lock_timeout_rolls_back_entire_publication`: 중간 공개 실패 시 묶음 전체 롤백 확인. 1초 제한은 테스트에서만 설정합니다.
- `test_target_without_date_index_is_currently_accepted`: 미해결 인덱스 검증 누락과 전체 스캔 재현.
- `test_target_load_batches_but_publication_is_one_statement`: 적재 배치 상한과 분할되지 않은 최종 반영 확인.

결과: 당시 단위/UI **42건**, MariaDB 통합 **28건** 통과. 위 두 위험 재현 테스트의 통과는 안전하다는 뜻이 아니라 현재 위험 동작이 확인되었다는 뜻입니다. 개선 시 기대값도 수정해야 합니다.

## 테스트 환경과 한계

아래 명령은 v0.1 시점의 단일 서버 재현 기록입니다. v0.2부터는 원본/대상을 별도 서버로 실행하고 `SNAPSHOT_TEST_TARGET_PORT`도 지정해야 합니다(README 참고).

당시 테스트 서버 실행 명령:

```sh
docker run -d --name snapshot-target-safety-check -p 127.0.0.1:33317:3306 \
  -e MARIADB_ROOT_PASSWORD=snapshot-test-only \
  --health-cmd='healthcheck.sh --connect --innodb_initialized' \
  --health-interval=2s --health-timeout=5s --health-retries=30 mariadb:11.4
```

`docker inspect --format '{{.State.Health.Status}}' snapshot-target-safety-check`가 `healthy`이면 실행합니다.

```sh
SNAPSHOT_TEST_PORT=33317 .venv/bin/pytest -q tests/test_integration.py
# 테스트 컨테이너와 테스트 데이터 제거
docker rm -f -v snapshot-target-safety-check
```

테스트는 연결한 서버의 `snapshot_source`와 `snapshot_target`을 삭제·재생성합니다. 반드시 별도 폐기용 서버만 지정하세요. 실제 환경의 CPU/I/O, 기존 업무 쿼리 응답시간, 복제 지연, 최대 행 크기, 디스크·로그 사용량의 허용치를 이 소규모 시험으로 보장하지 않습니다.

운영 적용 전에는 **날짜 인덱스 검증 → 대상 잠금/쿼리 시간 제한 → 적재 속도·전체 반영 크기 제한** 순으로 보강하는 것을 권장합니다. 최종 반영을 단순히 여러 번 커밋하면 현재의 테이블 묶음 원자성이 깨지므로 별도 설계가 필요합니다.
