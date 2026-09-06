# MariaDB Snapshot

**Developed with OpenAI Codex.** 사용자 요구사항을 바탕으로 Codex를 활용해 구현·테스트·문서를 작성했습니다.

선택한 MariaDB 테이블을 기준일별 분석 스냅샷으로 복사하는 로컬 도구입니다. Streamlit 화면, 별도 복사 프로세스, SQLite 설정·이력 저장소로 구성합니다.

## 로컬 실행

Python **3.11 이상**, 원본 MariaDB 접속 정보, 쓰기 가능한 별도 대상 DB/schema, 중간 파일용 디스크 공간이 필요합니다. 대상 schema는 미리 생성하세요. 스냅샷 테이블은 앱이 생성합니다. 일반 사용에는 Docker가 필요 없습니다.

프로젝트 폴더에서 실행합니다.

- **Windows**: `start-windows.bat` 더블클릭 또는 PowerShell에서 `.\start-windows.bat`
- **macOS**: 터미널에서 `./start-macos.sh`

최초 실행 시 가상환경과 패키지를 설치합니다(인터넷 필요). 이후 브라우저에서 **http://127.0.0.1:8501**을 엽니다. 다음 실행에도 같은 시작 파일을 사용합니다.

직접 설치·실행하거나 의존성을 업데이트하려면(macOS/Linux):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m streamlit run app.py --server.address 127.0.0.1
```

Windows에서는 `python3` 대신 `py -3`, `.venv/bin/python` 대신 `.venv\Scripts\python.exe`를 사용합니다.

앱 종료는 실행 창에서 **Ctrl+C**입니다. 브라우저나 앱 서버를 닫아도 별도 복사 프로세스는 계속 실행될 수 있으므로, 복사를 중단하려면 화면에서 **복사 취소**를 누르세요.

## 사용 순서

| 메뉴 | 할 일 |
| --- | --- |
| **1. 연결 설정** | 원본(Source), 대상(Target)을 각각 등록하고 연결 테스트. 필요하면 TLS 설정 |
| **2. 복사 작업** | 원본·대상 테이블, 읽기 방식, 배치 크기, 대기 시간 설정 |
| **3. 소량 테스트** | 적은 행으로 값·스키마·성능 확인. 이 단계도 대상 DB에 데이터를 저장 |
| **4. 전체 실행** | 작업, 기준일, 중간 파일 경로를 확인하고 수동 실행 |
| **5. 결과·복구** | 진행률·결과 확인, 취소, 재시도, 중간 파일 정리 |
| **작업 조회** | 저장된 작업과 최근 실행 결과를 검색하고 편집·실행으로 이동 |
| **실행 이력** | 기간·상태·작업별 실행 결과와 작업 내역 조회, CSV 저장 |

작업 생성·수정·삭제, 실행·재시도·취소 요청, 실행 상태 변경, 파일 정리를 기록합니다. 삭제된 작업의 과거 실행도 조회할 수 있습니다. 기록은 로컬 저장소 기준이며 변경 불가능한 감사 로그나 사용자 인증 시스템은 아닙니다.

## 실행 가능한 쿼리와 차단 범위

### 원본(Source)

쓰기 권한이 있는 계정도 **매 연결마다 읽기 전용 세션으로 설정**합니다. 설정·검증에 실패하면 즉시 연결을 닫고 작업을 실패 처리합니다. 재시도는 새 연결에서 동일하게 설정하며, 드라이버의 자동 재연결은 사용하지 않습니다.

| 구분 | 허용 / 차단 |
| --- | --- |
| 연결 초기화 | 드라이버의 문자셋·autocommit 설정, UTC `SET SESSION time_zone`, `SET SESSION TRANSACTION READ ONLY`, `SELECT @@session.tx_read_only` 검증만 내부 수행 |
| 서버 정보 | 고정된 `SELECT VERSION()`, `SELECT @@hostname, @@port, @@server_id, @@datadir` 허용 |
| 테이블·컬럼·인덱스 | 코드에 정의된 `information_schema.TABLES / COLUMNS / STATISTICS` 조회 템플릿만 허용 |
| 데이터 읽기 | 앱이 생성한 컬럼 조회, 키 비교 `WHERE`, `ORDER BY`, `LIMIT` 및 연결 테스트용 `SELECT * FROM 테이블 LIMIT 0` 허용 |
| 데이터 변경 | `INSERT`, `UPDATE`, `DELETE`, `REPLACE`, `LOAD DATA` 등 실행 인터페이스에서 차단 |
| 구조 변경 | `CREATE`, `ALTER`, `TRUNCATE`, `DROP`, `RENAME` 등 차단 |
| 위험한 조회 | `SELECT … INTO OUTFILE/DUMPFILE`, `FOR UPDATE`, `LOCK IN SHARE MODE`, 임의 함수·UDF·`SLEEP()` 호출 차단 |
| 기타 임의 SQL | `CALL`, 쓰기 모드로 변경하는 `SET`·`START TRANSACTION`, 주석·다중 문장, `SELECT 1`을 포함한 미등록 조회 모두 차단 |

SQL 입력창은 제공하지 않습니다. 메타데이터는 정확히 일치하는 템플릿만 받고, 데이터 조회는 테이블·컬럼명을 백틱으로 이스케이프하고 조건 값을 바인딩해 생성합니다. 읽기 결과에는 `execute()`나 연결 객체를 노출하지 않습니다. 단순히 `SELECT`로 시작하는지만 검사하지 않습니다.

**한계:** 이 보호는 앱의 원본 경로에 적용됩니다. 읽기 전용 세션은 계정 권한을 제거하지 않으므로, 다른 프로그램이나 수정된 코드가 쓰기 모드로 바꾸면 우회할 수 있습니다. 가장 강한 보호는 별도 **SELECT 전용 계정**입니다. 서버 전체의 `read_only` 설정이나 기존 계정 권한은 앱에서 변경하지 않습니다. 읽기 쿼리도 CPU·I/O·메타데이터 잠금에 영향을 줄 수 있으므로 배치 크기와 대기 시간을 조정하세요.

### 대상(Target)

대상은 복사를 위해 쓰기가 필요하며 Source의 SQL 제한을 적용하지 않습니다. 아래는 앱이 수행하는 명령 범위입니다.

| 용도 | 수행하는 명령 |
| --- | --- |
| 연결·검증 | 세션 설정(UTC·엄격한 SQL 모드), 메타데이터·행 수·실행 마커 `SELECT` |
| 준비·적재 | 스냅샷·staging·실행 마커 `CREATE TABLE`, `INSERT` / `INSERT … SELECT` |
| 기준일 교체 | `DELETE … WHERE snapshot_date = %s`, `INSERT`, 트랜잭션 시작·`COMMIT`·`ROLLBACK` |
| 동시 실행·복구 | `GET_LOCK`(연결 종료 시 반환), 실행 마커 조회 |
| 정리·연결 테스트 | 앱의 staging 테이블 `DROP TABLE`, 테스트용 임시 테이블 생성·적재·삭제·제거 |

앱은 대상에 `UPDATE`나 `TRUNCATE`를 사용하지 않지만, 대상 계정 자체의 SQL 방화벽을 제공하는 것은 아닙니다. **대상 계정의 권한은 전용 분석 schema로 제한하세요.** 원본과 대상이 동일 서버·동일 schema로 확인되면 쓰기 전에 차단하지만, 잘못 등록한 별도 운영 schema까지 판별하지는 못합니다.

## 복사·복구 규칙

- 원본 **InnoDB** 테이블을 지원합니다. PK 또는 NOT NULL 전체 UNIQUE 키가 있으면 키 기반 배치 조회, 키가 없으면 스트리밍을 사용합니다.
- 대상에 `snapshot_date`를 추가합니다. 원본에 같은 컬럼이 있거나 대상 스키마가 맞지 않으면 차단합니다. 원본의 생성 컬럼은 값으로 저장하며, 트리거·FK·CHECK 등은 복제하지 않습니다.
- 원본을 중간 파일로 추출하고 대상 staging에 적재·검증한 후, 선택한 모든 테이블의 해당 기준일 데이터를 하나의 트랜잭션으로 교체합니다. 다른 날짜는 유지합니다. 한 실행은 같은 Target 연결을 사용합니다.
- 원본 전체의 단일 시점 일관성은 보장하지 않습니다. 원본 갱신이 적은 시간에 실행하세요. 최초 실패 시 대상에 빈 테이블이 남을 수 있습니다.
- 실패 시 검증된 완료 파일을 재사용합니다. 커밋 응답 유실은 실행 마커로 복구하며, 상태가 불확실하면 결과·복구 화면에서 확인합니다. 강제 종료는 일반 취소가 끝나지 않을 때 사용합니다.

## 저장 위치와 비밀번호

기본 `.snapshot/`에 설정·실행 이력을 저장합니다. 시작 전에 `SNAPSHOT_HOME`을 지정하면 위치를 변경할 수 있습니다. 이 폴더를 삭제하면 설정과 이력을 잃습니다.

비밀번호는 세션 메모리 또는 선택한 OS 키체인에 보관하며 SQLite·설정 내보내기에 포함하지 않습니다. 중간 파일에는 원본 데이터가 들어 있으므로 접근 가능한 폴더를 제한하세요. 성공하면 중간 데이터 파일을 정리하고 이력·로그를 남깁니다. 실패 파일은 재시도용으로 보관합니다. 앱은 로컬 개인 사용을 전제로 하므로 외부에 공개하지 마세요.

## 로컬 테스트 DB

Docker Desktop을 실행한 뒤 프로젝트 폴더에서 준비 스크립트를 실행합니다. Python 표준 라이브러리만 사용하므로 별도 패키지 설치는 필요 없습니다.

```sh
# macOS / Linux
python3 scripts/start_demo_db.py
```

Windows에서는 `py -3 scripts/start_demo_db.py`를 실행합니다. 어느 폴더에서 호출해도 프로젝트의 Docker 설정을 사용합니다.

스크립트가 MariaDB 11.4를 시작하고 준비 완료를 기다린 뒤 원본·대상 DB와 아래 합성 데이터를 생성합니다. 재실행하면 누락된 샘플 ID만 추가하며 기존 행과 대상 데이터는 유지합니다.

| 원본 테이블 | 최초 생성 행 수 | 확인할 내용 |
| --- | ---: | --- |
| `demo_customers` | 100 | 한글·이모지·NULL |
| `demo_orders` | 1,200 | 배치 분할·소수 금액·날짜·개행 |
| `demo_order_items` | 2,400 | 복합 PK |

앱에서 원본·대상 연결을 저장하고 접속 확인 후, **복사 작업**에서 위 테이블을 선택하세요. 읽기 방식은 **PK 배치**, 배치 크기는 **100**으로 시작하고 **소량 테스트 → 전체 실행 → 결과·복구** 순서로 확인합니다. 대상 스냅샷 테이블은 앱이 생성합니다.

DBeaver 등 DB 클라이언트나 앱의 연결 설정에서 다음 정보를 사용합니다.

| 항목 | 값 |
| --- | --- |
| 호스트 | `127.0.0.1` |
| 포트 | `33316` |
| 사용자 | `root` |
| 비밀번호 | `snapshot-test-only` |
| 앱 DB명 (필수) | Source: `snapshot_source` / Target: `snapshot_target` |

CLI 접속은 다음 명령을 실행하고 위 비밀번호를 입력합니다.

```sh
docker compose -f compose.test.yml exec mariadb mariadb -u root -p
```

DB와 데이터는 준비 스크립트가 생성하므로 별도 SQL 입력이 필요 없습니다. 생성 SQL은 [scripts/demo_data.sql](scripts/demo_data.sql)에서 확인할 수 있습니다.

**공개된 로컬 테스트 전용 계정이며 운영 환경 사용 금지입니다.** 비밀번호를 운영·공유 DB에서 재사용하거나 실제 운영 데이터·개인정보를 저장하지 마세요. [compose.test.yml](compose.test.yml)의 `127.0.0.1` 바인딩을 유지하고 외부에 포트를 공개하지 마세요.

일시 중지는 `docker compose -f compose.test.yml stop`, 컨테이너와 테스트 데이터 정리는 `docker compose -f compose.test.yml down -v`입니다.

## 개발 검증

```sh
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/ruff check .
.venv/bin/pytest -q -m 'not integration'
docker compose -f compose.test.yml up -d --wait
SNAPSHOT_TEST_PORT=33316 .venv/bin/pytest -q tests/test_integration.py
docker compose -f compose.test.yml down -v
```

**통합 테스트는 `snapshot_source`·`snapshot_target` DB를 삭제·재생성합니다. 반드시 제공된 폐기용 Docker 서버에서만 실행하세요.** 고정 비밀번호는 테스트 전용입니다. 쓰기 권한이 있는 root 연결에서도 원본 DML·DDL이 거부되는지 검증합니다.

macOS / Python 3.12 / MariaDB 11.4 검증 및 성능 기록은 [VALIDATION.md](VALIDATION.md)를 참고하세요. Windows 실기기 검증은 별도이며 CI 설정을 제공합니다.
