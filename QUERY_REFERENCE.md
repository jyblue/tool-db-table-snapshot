# SQL 실행 목록

이 문서는 애플리케이션이 실행하는 SQL을 기능 리뷰용으로 한 줄씩 정리한 목록입니다. 기준은 현재 코드이며, SQL 문자열은 실행 시 식별자와 값이 채워질 수 있습니다. 같은 템플릿을 반복 실행하는 경우는 템플릿 한 줄로 묶고, 바인딩 값·테이블 번호처럼 데이터만 다른 부분은 생략했습니다.

- MariaDB SQL은 `snapshot/db.py`, `snapshot/engine.py`, `snapshot/compare.py`, `snapshot/process.py`의 실행 경로를 기준으로 합니다.
- `%s` 값은 PyMySQL 바인딩으로 전달하고, 테이블·컬럼명은 `db.ident()`로 백틱 이스케이프합니다.
- SQLite SQL은 앱의 로컬 설정·작업·이력 저장소에만 사용합니다.
- `scripts/`와 `tests/`의 SQL은 폐기 가능한 Docker 테스트 DB 또는 테스트 전용 SQLite에만 실행됩니다.

## 1. 원본(Source) 연결과 읽기

| 위치 | 실행 SQL | 한 줄 설명 |
| --- | --- | --- |
| `db.connect()` | `SET SESSION time_zone='+00:00'` | 날짜·시간 값을 두 DB에서 UTC 기준으로 해석합니다. |
| `configure_read_only()` | `SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED` | 원본 읽기 트랜잭션의 격리 수준을 설정합니다. |
| `configure_read_only()` | `SET SESSION TRANSACTION READ ONLY` | 원본 세션의 트랜잭션 쓰기를 읽기 전용으로 설정합니다. |
| `configure_read_only()` | `SET SESSION max_statement_time=2, lock_wait_timeout=1, innodb_lock_wait_timeout=1, net_write_timeout=2, wait_timeout=30` | 원본 쿼리·잠금·네트워크·유휴 연결의 상한을 설정합니다. |
| `configure_read_only()` | `SELECT @@session.tx_read_only, @@session.tx_isolation, @@autocommit, @@max_statement_time, @@lock_wait_timeout, @@innodb_lock_wait_timeout, @@net_write_timeout, @@wait_timeout` | 위 보호 설정이 실제 세션에 적용됐는지 확인합니다. |
| `Source.__init__()` | `SELECT GET_LOCK(%s, 0)` | 같은 원본 서버의 앱 조회를 동시에 하나만 허용하는 비블로킹 애플리케이션 mutex를 얻습니다. |
| `identity()` | `SELECT @@hostname, @@port, @@server_id, @@datadir` | Source와 Target이 같은 MariaDB 인스턴스인지 식별합니다. |
| `test_connection()` | `SELECT VERSION()` | 연결된 MariaDB 버전을 표시합니다. |
| `wsrep_enabled()` | `SHOW VARIABLES LIKE 'wsrep_on'` | Galera 활성 인스턴스를 차단하기 위해 상태를 확인합니다. |
| `tables()` | `SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME LIMIT 1001 ROWS EXAMINED 2000` | 원본 schema의 기본 테이블 목록을 제한해서 읽습니다. |
| `schema()` | `SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s` | 선택한 테이블이 InnoDB인지 확인합니다. |
| `schema()` | `SELECT COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,CHARACTER_SET_NAME,COLLATION_NAME,EXTRA FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION` | 컬럼 순서·타입·NULL·문자셋·생성 속성을 읽습니다. |
| `schema()` | `SELECT INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME,SUB_PART FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY INDEX_NAME,SEQ_IN_INDEX` | PK 또는 NOT NULL 전체 UNIQUE 키와 인덱스 구성을 읽습니다. |
| `Source.read()` | `EXPLAIN SELECT SQL_NO_CACHE <columns> FROM <table> [WHERE <keyset>] [ORDER BY <key>] LIMIT %s` | 키셋 배치가 인덱스 접근·정렬 없이 실행되는지 먼저 확인합니다. |
| `Source.read()` | `SELECT SQL_NO_CACHE <columns> FROM <table> [WHERE <keyset>] [ORDER BY <key>] LIMIT %s ROWS EXAMINED 2000` | 생성된 조건으로 원본 행을 최대 1,000행 단위로 읽고 조사량을 제한합니다. |
| `prepare()`(Source schema 재확인) | 위 `ENGINE`·`COLUMNS`·`STATISTICS` 조회 3개 | 추출 전후 원본 schema가 바뀌지 않았는지 확인합니다. |

Source에는 임의 SQL 입력 경로가 없습니다. Source cursor에 도달하는 고정 SQL은 위 목록과 앱이 생성한 `EXPLAIN`·`SELECT`뿐이며, Source에서 `INSERT`, `UPDATE`, `DELETE`, `CREATE`, `ALTER`, `TRUNCATE`, `DROP`, `LOCK TABLES`, `SELECT ... INTO`를 실행하지 않습니다.

## 2. 대상(Target) 연결, 준비와 적재

Target에서도 연결·격리 검사를 위해 위의 `VERSION`, `IDENTITY`, `WSREP`, `TABLES`, `COLUMNS`, `STATISTICS` 계열 조회를 사용합니다. 다음은 Target에만 실행되는 명령입니다.

| 위치 | 실행 SQL | 한 줄 설명 |
| --- | --- | --- |
| `db.connect(target=True)` | `SET SESSION time_zone='+00:00'` 및 `sql_mode=STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_AUTO_VALUE_ON_ZERO` | 적재 값의 시간대와 타입 오류 처리를 고정합니다. (`sql_mode`는 연결 옵션으로 전송됩니다.) |
| `prepare()` | `SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s` | 대상 테이블이 이미 있는지 확인합니다. |
| `prepare()` | `CREATE TABLE <table> (<metadata-derived columns>, PRIMARY KEY (...)) ENGINE=InnoDB` | 없는 스냅샷·실행 마커 테이블을 원본 metadata로 생성합니다. |
| `prepare()` | `SELECT 1 FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=%s AND EVENT_OBJECT_TABLE=%s` | 트리거가 있는 대상 테이블을 차단합니다. |
| `prepare()` | `SELECT 1 FROM information_schema.TABLE_CONSTRAINTS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND CONSTRAINT_TYPE IN ('FOREIGN KEY','CHECK')` | FK·CHECK 제약이 있는 대상 테이블을 차단합니다. |
| `prepare()` | `SELECT 1 FROM information_schema.KEY_COLUMN_USAGE WHERE REFERENCED_TABLE_SCHEMA=%s AND REFERENCED_TABLE_NAME=%s` | 다른 테이블에서 참조되는 대상 테이블을 차단합니다. |
| `load()` | `DROP TABLE IF EXISTS <_snapshot_s_<run_id>_<n>>` | 현재 실행의 고유 staging 테이블만 재시도 전에 정리합니다. |
| `load()` | `CREATE TABLE <_snapshot_s_<run_id>_<n>> (...) ENGINE=InnoDB` | 대상에 원본 schema와 snapshot 날짜 컬럼을 가진 staging을 만듭니다. |
| `load()` | `INSERT INTO <stage> (<columns>) VALUES (%s,...)` (배치 `executemany`) | 중간 파일의 값을 staging에 바인딩해 적재합니다. |
| `load()` | `SELECT COUNT(*) FROM <stage>` | staging 행 수가 추출 manifest와 같은지 확인합니다. |
| `acquire_lock()` | `SELECT GET_LOCK(%s, %s)` | 같은 대상 schema에 대한 공개·복구 작업을 직렬화합니다. |

`begin()`, `commit()`, `rollback()`은 PyMySQL 연결의 트랜잭션 제어 호출입니다. 별도 SQL 문자열로 조합하지 않지만 DB에서는 각각 트랜잭션 시작·확정·취소로 동작합니다.

## 3. 대상 공개(Publish)와 복구

| 위치 | 실행 SQL | 한 줄 설명 |
| --- | --- | --- |
| `Engine.execute()` | `DELETE FROM <target_table> WHERE snapshot_date=%s` | 선택한 기준일의 기존 스냅샷 행만 교체 전에 삭제합니다. |
| `Engine.execute()` | `INSERT INTO <target_table> SELECT * FROM <stage>` | 검증된 staging 행을 정식 대상 테이블에 공개합니다. |
| `Engine.execute()` | `INSERT INTO _snapshot_runs VALUES (%s,%s,%s)` | run ID·기준일·대상 테이블 목록을 공개 마커로 기록합니다. |
| `reconcile()` | `SELECT snapshot_date,tables_json FROM _snapshot_runs WHERE run_id=%s` | 커밋 응답이 유실된 경우 공개 성공 여부를 마커로 확인합니다. |
| `Engine.execute()` finally / `cleanup()` | `DROP TABLE IF EXISTS <_snapshot_s_<run_id>_<n>>` | 공개가 끝났거나 사용자가 정리할 때 앱이 만든 staging만 삭제합니다. |
| `test_connection()` | `CREATE TEMPORARY TABLE _snapshot_permission_test (n INT) ENGINE=InnoDB` | 대상 계정의 임시 객체 생성 권한만 확인합니다. |
| `test_connection()` | `INSERT INTO _snapshot_permission_test VALUES (1)` | 임시 테이블 적재 권한을 확인합니다. |
| `test_connection()` | `SELECT n FROM _snapshot_permission_test` | 임시 테이블 읽기 권한을 확인합니다. |
| `test_connection()` | `DELETE FROM _snapshot_permission_test` | 테스트 행을 임시 테이블에서만 제거합니다. |
| `test_connection()` | `DROP TEMPORARY TABLE _snapshot_permission_test` | 권한 테스트 임시 객체를 연결 종료 전에 제거합니다. |

정식 공개의 `DELETE`는 `snapshot_date`가 같은 대상 행으로 범위가 고정되고, 삭제·삽입·마커 기록은 하나의 트랜잭션에서 수행됩니다. Target 계정은 전용 schema로 제한해야 하며 앱은 Target 전체에 대한 SQL 방화벽이 아닙니다.

## 4. 날짜별 비교

| 위치 | 실행 SQL | 한 줄 설명 |
| --- | --- | --- |
| `target_tables()` | `SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME` | 대상에서 비교 가능한 사용자 테이블 목록을 읽습니다. `_snapshot_` 내부 테이블은 화면에서 제외합니다. |
| `_metadata()` | `ENGINE`, `COLUMNS`, `STATISTICS` 조회 3개 | 비교 테이블의 `snapshot_date`와 PK/UNIQUE·선두 인덱스를 확인합니다. |
| `available_dates()` | `SELECT DISTINCT snapshot_date FROM <table> WHERE snapshot_date IS NOT NULL ORDER BY snapshot_date DESC LIMIT %s ROWS EXAMINED 10000` | 선택한 대상 테이블에서 비교 가능한 기준일 목록을 제한해서 읽습니다. |
| `compare()` | `SELECT <all columns> FROM <table> WHERE snapshot_date=%s ORDER BY <key> LIMIT %s ROWS EXAMINED <examined_limit>` | 두 기준일을 각각 최대 비교 행 수만큼 키 순서로 읽어 추가·삭제·변경을 계산합니다. |

비교는 DB를 변경하지 않습니다. 동일한 row는 결과에서 제외하고, 결과 최대 건수는 추가·삭제·변경 결과 전체에 적용합니다.

## 5. 로컬 SQLite 저장소

앱의 설정·실행·로그는 MariaDB가 아니라 `.snapshot/state.sqlite3`에 저장됩니다.

| 위치 | 실행 SQL | 한 줄 설명 |
| --- | --- | --- |
| `Store.__init__()` | `PRAGMA journal_mode=WAL` | 로컬 이력 읽기와 쓰기를 안전하게 분리합니다. |
| `Store.__init__()` | `CREATE TABLE IF NOT EXISTS connection_profile(...)` | Source/Target 연결 profile을 저장할 테이블을 만듭니다. |
| `Store.__init__()` | `CREATE TABLE IF NOT EXISTS backup_job(...)` | 복사 작업 정의를 저장할 테이블을 만듭니다. |
| `Store.__init__()` | `CREATE TABLE IF NOT EXISTS run(...)` | 실행 상태·시간·취소·복구 정보를 저장할 테이블을 만듭니다. |
| `Store.__init__()` | `CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON run((1)) WHERE state IN (...)` | 동시에 하나의 실행만 활성화되도록 보장합니다. |
| `Store.__init__()` | `CREATE TABLE IF NOT EXISTS run_table(...)` | 실행별 테이블 진행률과 staging·파일 상태를 저장합니다. |
| `Store.__init__()` | `CREATE INDEX IF NOT EXISTS run_created ON run(created DESC, id DESC)` | 최근 실행 목록을 빠르게 조회합니다. |
| `Store.__init__()` | `CREATE INDEX IF NOT EXISTS run_table_job ON run_table(job_id,run_id)` | 작업별 실행 이력을 빠르게 조회합니다. |
| `Store.__init__()` | `CREATE TABLE IF NOT EXISTS activity_log(...)` | 작업 생성·실행·취소·정리 이벤트를 기록합니다. |
| `Store.__init__()` | `CREATE INDEX IF NOT EXISTS activity_run ON activity_log(run_id,id DESC)` | 실행별 로그 조회를 빠르게 합니다. |
| `Store.db()` | `PRAGMA foreign_keys=ON` | SQLite 연결에서 외래키 검사를 켭니다. |
| `profiles()` / `jobs()` | `SELECT data FROM <connection_profile|backup_job> ORDER BY rowid` | 저장된 연결 또는 작업을 목록으로 읽습니다. |
| `save()` | `SELECT 1 FROM <table> WHERE id=?` | 연결·작업이 신규인지 기존인지 확인합니다. |
| `save()` | `INSERT OR REPLACE INTO <table> VALUES (?,?)` | 비밀번호를 제외한 연결·작업 JSON을 저장합니다. |
| `delete()` | `SELECT data FROM backup_job` | 연결을 사용하는 작업이 있는지 확인합니다. |
| `delete()` | `SELECT data FROM <table> WHERE id=?` | 삭제할 작업의 이름과 존재 여부를 읽습니다. |
| `delete()` | `DELETE FROM <table> WHERE id=?` | 유휴 상태에서 선택한 로컬 설정을 삭제합니다. |
| `_assert_idle()` | `SELECT 1 FROM run WHERE state IN (...) OR publish_pending=1` | 실행 중이거나 공개 확인이 필요한 동안 설정 변경을 막습니다. |
| `queue()` | `INSERT INTO run(id,state,spec,created,retry_of) VALUES (?,?,?,?,?)` | 실행 요청과 재시도 관계를 기록합니다. |
| `queue()` | `INSERT INTO run_table(run_id,ordinal,job_id) VALUES (?,?,?)` | 실행에 포함된 작업 목록을 기록합니다. |
| `_event()` | `INSERT INTO activity_log(occurred,actor,action,entity_id,name,run_id,detail) VALUES (?,?,?,?,?,?,?)` | 사용자·시스템 동작을 감사용 활동 이력에 추가합니다. |
| `runs()` | `SELECT * FROM run ORDER BY created DESC LIMIT 100` | 최근 실행 100건을 표시합니다. |
| `run()` | `SELECT * FROM run WHERE id=?` | 한 실행의 상세 정보와 JSON 명세를 읽습니다. |
| `tables()` | `SELECT * FROM run_table WHERE run_id=? ORDER BY ordinal` | 한 실행의 테이블별 진행 상태를 읽습니다. |
| `update_run()` | `SELECT state FROM run WHERE id=?` | 상태 변경 이벤트를 남기기 위해 이전 상태를 읽습니다. |
| `update_run()` | `UPDATE run SET <허용 필드>=? WHERE id=?` | 실행 상태·진행·오류·복구 플래그를 갱신합니다. |
| `update_table()` | `UPDATE run_table SET <허용 필드>=? WHERE run_id=? AND ordinal=?` | 테이블별 단계·행 수·파일 상태를 갱신합니다. |
| `cancel()` | `UPDATE run SET cancel=1,state='CANCEL_REQUESTED' WHERE id=? AND state IN (...)` | 활성 실행에 정상 취소를 요청합니다. |
| `begin_publish()` | `SELECT cancel FROM run WHERE id=?` | 공개 직전에 취소 요청이 들어왔는지 확인합니다. |
| `begin_publish()` | `UPDATE run SET state='PUBLISHING',publish_pending=1 WHERE id=?` | 공개 트랜잭션이 시작될 예정임을 기록합니다. |
| `interrupt_if_current()` | `UPDATE run SET state='INTERRUPTED',ended=?,error=?,server_status=? WHERE id=? AND pid IS ? AND process_created IS ? AND state IN (...)` | 같은 worker가 사라진 경우에만 실행을 중단 상태로 표시합니다. |
| `activities()` | `SELECT COUNT(*) FROM activity_log [WHERE run_id=?]` | 활동 로그 전체 건수를 계산합니다. |
| `activities()` | `SELECT * FROM activity_log [WHERE run_id=?] ORDER BY id DESC LIMIT ? OFFSET ?` | 활동 로그를 페이지 단위로 읽습니다. |
| `history()` | `SELECT COUNT(*) FROM run r [WHERE ...]` | 검색·상태·작업·기간 필터에 맞는 실행 수를 계산합니다. |
| `history()` | `SELECT r.*,(SELECT COUNT(*) ...),(SELECT COALESCE(SUM(extracted),0) ...),(SELECT COALESCE(SUM(loaded),0) ...) FROM run r [WHERE ...] ORDER BY r.created DESC,r.id DESC LIMIT ? OFFSET ?` | 실행 목록과 테이블 수·추출·적재 합계를 함께 페이지로 읽습니다. |
| `history()` 키워드 필터 | `instr(lower(...))`, `json_each(r.spec,'$.jobs')`, `json_extract(...)`, `coalesce(...)` | 실행 ID·작업명·원본/대상 테이블을 JSON 명세 안에서 검색합니다. |
| `job_overview()` | `WITH ranked AS (... COUNT(*) OVER (...) ... ROW_NUMBER() OVER (...)) SELECT * FROM ranked WHERE position=1` | 작업별 최신 실행과 누적 실행 수를 계산합니다. |
| `worker.main()` | `BEGIN IMMEDIATE` | worker가 상태를 선점하는 SQLite 쓰기 트랜잭션을 시작합니다. |
| `worker.main()` | `UPDATE run SET pid=?,process_created=?,started=?,heartbeat=?,state=CASE ... WHERE id=? AND pid IS NULL AND state IN (...)` | 동일 실행을 다른 worker가 중복 처리하지 않도록 원자적으로 선점합니다. |

SQLite의 `BEGIN IMMEDIATE`, `COMMIT`, `ROLLBACK`도 로컬 상태 저장소의 트랜잭션 제어이며 MariaDB 대상 데이터에는 영향을 주지 않습니다.

## 6. 데모·성능·통합 테스트 전용 SQL

아래 SQL은 제품 실행 경로가 아니라 폐기 가능한 테스트 자원 준비·검증에만 사용됩니다. 운영 DB에 실행하지 마세요.

| 파일/범위 | SQL 유형 | 한 줄 설명 |
| --- | --- | --- |
| `scripts/demo_data.sql` | `CREATE DATABASE IF NOT EXISTS snapshot_source`, `CREATE TABLE IF NOT EXISTS demo_customers/demo_orders/demo_order_items` | 두 개 이상의 원본 테이블과 샘플 schema를 만듭니다. |
| `scripts/demo_data.sql` | `START TRANSACTION`, `INSERT ... SELECT ... seq_1_to_*`, `COMMIT` | 누락된 데모 ID만 추가해 100·1,200·2,400행을 준비합니다. |
| `scripts/demo_data.sql` | `SELECT ... UNION ALL ...` | 데모 테이블별 행 수를 출력합니다. |
| `scripts/target_db.sql` | `CREATE DATABASE IF NOT EXISTS snapshot_target`, `CREATE TABLE IF NOT EXISTS demo_compare`, `INSERT IGNORE ...` | 날짜별 비교 예시의 두 기준일 데이터를 대상에 준비합니다. |
| `scripts/start_demo_db.py` | `SELECT @@hostname, @@port, @@server_id` | Source·Target Docker 컨테이너가 실제로 다른 인스턴스인지 확인합니다. |
| `tests/test_integration.py` fixture | `DROP/CREATE DATABASE snapshot_source`, `DROP USER`, `CREATE USER`, `GRANT SELECT`, `CREATE TABLE`, `INSERT` | 명시적 reset 환경에서만 폐기 가능한 Source fixture를 재생성합니다. |
| `tests/test_integration.py` assertions | `SELECT COUNT(*)`, `SELECT ... FROM information_schema...`, `SELECT ... FROM <target>` | 복사 결과·권한·서버 분리를 검증합니다. |
| `tests/test_integration.py` safety cases | `DELETE`, `UPDATE`, `ALTER`, `DROP`, `TRUNCATE`, `LOCK TABLES`, `FOR UPDATE` 등 | 원본 보호 테스트에서 위험 SQL이 실행 경로에 도달하지 않거나 거부되는지 검증합니다. 테스트 fixture 밖의 운영 DB에는 실행하지 않습니다. |
| `tests/test_integration.py` safety fixtures | `SET SESSION innodb_lock_wait_timeout=1`, `SET SESSION lock_wait_timeout=1`, `SELECT SLEEP(10)`, `UPDATE ...`, `LOCK TABLES ... WRITE`, `INSERT ... SELECT ...` | 잠금 대기·느린 읽기·대형 행·DDL 잠금 상황에서 중단과 원본 불변성을 검증합니다. 모두 폐기용 Docker DB에서만 실행합니다. |
| `tests/test_core.py` rejection inputs | `DELETE ...`, `UPDATE ...`, `TRUNCATE ...`, `DROP ...`, `INSERT ...`, `ALTER ...`, `SET ... READ WRITE`, `START TRANSACTION READ WRITE`, `SELECT 1`, 다중 문장, `INTO OUTFILE`, `FOR UPDATE`, `LOCK IN SHARE MODE`, 위험 함수 호출 | Source의 고정 템플릿 검사가 임의·쓰기·잠금 SQL을 DB로 보내기 전에 거부하는지 단위 검증합니다. |
| `tests/test_history.py` | `UPDATE run SET created=? WHERE id=?`, `DROP TABLE activity_log` | 정렬·복구 경로와 활동 로그 누락 시나리오를 재현하는 SQLite 테스트 전용 SQL입니다. |
| `scripts/benchmark.py` | `CREATE DATABASE`, `CREATE USER`, `GRANT SELECT`, `CREATE TABLE`, `INSERT`, `ANALYZE TABLE`, `SHOW GLOBAL STATUS`, `SELECT SUM(DATA_LENGTH+INDEX_LENGTH)` | 별도 이름의 벤치마크 DB를 만들고 적재량·InnoDB 로그 증가량을 측정합니다. |

테스트 코드의 위험 SQL은 테스트 DB를 초기화하거나 방어 로직을 검증하기 위한 입력입니다. 애플리케이션의 Source 연결 객체로 전달되는 경로와는 분리되어 있습니다.

## 빠른 리뷰 포인트

1. Source 쓰기·구조 변경 SQL은 제품 실행 목록에 없습니다.
2. Source 데이터 조회는 앱이 만든 식별자·바인딩 값·키셋 조건으로만 생성됩니다.
3. Target의 영구 변경은 staging 생성·적재와 기준일 `DELETE` 후 `INSERT` 공개로 한정됩니다.
4. Target 공개·복구는 `GET_LOCK`과 실행 마커를 사용하고, 공개 데이터 변경은 한 트랜잭션으로 처리합니다.
5. 사용자 작업·실행·활동 이력은 별도 SQLite에 저장되며 MariaDB 원본과 분리됩니다.
