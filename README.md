# MariaDB Snapshot

**Developed with OpenAI Codex.** 사용자 요구사항을 바탕으로 Codex를 활용해 구현·테스트·문서를 작성했습니다.

선택한 MariaDB 테이블을 기준일별 분석 스냅샷으로 복사하는 로컬 도구입니다. Streamlit 화면, 별도 복사 프로세스, SQLite 설정·이력 저장소로 구성합니다.

실행되는 SQL의 전체 목록과 한 줄 설명은 [QUERY_REFERENCE.md](QUERY_REFERENCE.md)에서 확인할 수 있습니다.

## 로컬 실행

Git 설치 없이 [최신 Release](https://github.com/jyblue/tool-db-table-snapshot/releases/latest)에서 **`mariadb-snapshot-v0.2.9.zip`**을 다운로드하고 압축을 푸세요. 아래 명령은 압축을 푼 프로젝트 폴더에서 실행합니다.

Python **3.10 이상**, 원본 MariaDB 접속 정보, 별도 MariaDB 인스턴스의 쓰기 가능한 대상 DB/schema, 중간 파일용 디스크 공간이 필요합니다. 대상 schema는 미리 생성하세요. 스냅샷 테이블은 앱이 생성합니다. 일반 사용에는 Docker가 필요 없습니다.

프로젝트 폴더에서 실행합니다.

- **Windows**: `start-windows.bat` 더블클릭 또는 PowerShell에서 `.\start-windows.bat`
- **macOS**: 터미널에서 `./start-macos.sh`

최초 실행 시 가상환경과 패키지를 설치합니다(패키지 저장소 접근 필요). 사내 Python 저장소를 사용하도록 pip를 구성한 뒤 실행하세요. 이후 브라우저에서 **http://127.0.0.1:8501**을 엽니다. 다음 실행에도 같은 시작 파일을 사용합니다.

직접 설치·실행하거나 의존성을 업데이트하려면(macOS/Linux):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m streamlit run app.py --server.address 127.0.0.1
```

Windows에서 직접 설치할 때는 다음처럼 실행합니다.

```bat
python -c "import sys; assert sys.version_info >= (3, 10), sys.version"
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1
```

위 명령의 `python`이 3.10 미만이면 설치된 3.10 이상 인터프리터로 바꾸세요(예: `py -3.12`). `start-windows.bat`은 Python Launcher에 등록된 3.10 이상 버전을 자동으로 찾아 사용하며, 실행 중에는 `setuptools` editable build를 사용하지 않습니다. 설치 실패 시 pip의 실제 오류를 화면에 남깁니다.

Windows에서 `setuptools` 버전을 찾을 수 없다는 오류가 나면 `.venv`를 삭제하고 `start-windows.bat`을 다시 실행하세요. 시작 파일은 `setuptools`가 필요한 editable 설치 대신 `requirements.txt`의 실행용 wheel만 설치합니다. `Python 3.10 or newer was not found`가 나오면 `py -0p`로 설치된 인터프리터를 확인하고 Python 3.10 이상을 Python Launcher와 함께 설치하세요.

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
| **날짜별 비교** | 대상 테이블의 두 기준일을 선택해 추가·삭제·변경 행을 비교 |

작업 생성·수정·삭제, 실행·재시도·취소 요청, 실행 상태 변경, 파일 정리를 기록합니다. 삭제된 작업의 과거 실행도 조회할 수 있습니다. 기록은 로컬 저장소 기준이며 변경 불가능한 감사 로그나 사용자 인증 시스템은 아닙니다.

대상 DB를 변경하거나 작업·연결·중간 파일을 삭제하는 동작, 재시도·강제 중단 전에는 예상 동작과 결과를 확인하는 팝업이 표시됩니다. 확인해야 실제 동작이 실행됩니다.

## 실행 가능한 쿼리와 차단 범위

### 원본(Source)

쓰기 권한이 있는 계정도 **매 연결마다 읽기 전용 세션으로 설정**합니다. 설정·검증에 실패하면 연결을 닫고 작업을 중단합니다. 원본 추출은 통신 오류가 나도 자동으로 처음부터 재조회하지 않습니다. 수동 재시도 시에도 모든 제한을 다시 적용합니다.

| 구분 | 허용 / 차단 |
| --- | --- |
| 연결 초기화 | 드라이버의 문자셋·autocommit 설정, UTC `SET SESSION time_zone`, `READ ONLY`·`READ COMMITTED`, 실행/잠금/통신 제한 설정 및 세션 값 검증을 내부 수행 |
| 서버 정보 | 고정된 `SELECT VERSION()`, 서버 식별자 조회, Galera 활성 여부 확인 허용 |
| 테이블·컬럼·인덱스 | 코드에 정의된 `information_schema.TABLES / COLUMNS / STATISTICS` 조회 템플릿만 허용 |
| 데이터 읽기 | 앱이 생성한 `SELECT SQL_NO_CACHE`, 키 비교 `WHERE`, `ORDER BY`, `LIMIT … ROWS EXAMINED`와 해당 쿼리의 `EXPLAIN`만 허용 |
| 데이터 변경 | `INSERT`, `UPDATE`, `DELETE`, `REPLACE`, `LOAD DATA` 등 실행 인터페이스에서 차단 |
| 구조 변경 | `CREATE`, `ALTER`, `TRUNCATE`, `DROP`, `RENAME` 등 차단 |
| 위험한 조회 | `SELECT … INTO OUTFILE/DUMPFILE`, `FOR UPDATE`, `LOCK IN SHARE MODE`, 임의 함수·UDF·`SLEEP()` 호출 차단 |
| 동시 조회 제한 | 앱 전용 `GET_LOCK(…, 0)`으로 서버당 Source 연결 1개만 허용. 테이블/행 잠금이 아니며 연결 종료 시 반환 |
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

앱은 대상에 `UPDATE`나 `TRUNCATE`를 사용하지 않지만, 대상 계정 자체의 SQL 방화벽을 제공하는 것은 아닙니다. **대상 계정의 권한은 전용 분석 schema로 제한하세요.** 원본과 대상이 같은 MariaDB 인스턴스이면 schema가 달라도 차단합니다. 대상 재연결·복구·정리 시에도 다시 검사합니다. 다른 운영 서버를 잘못 등록한 경우까지 판별하지는 못합니다.

대상 적재 속도·전체 반영 크기의 상한과 대상 DB 용량 감시는 아직 제공하지 않습니다. 날짜 인덱스 누락 및 잠금 충돌 테스트 결과와 보강 항목은 [대상 DB 안전성 점검](TARGET_SAFETY.md)을 확인하세요.

### 원본 DB 부하·운영 리스크 최소화

- 가능하면 원본 운영 서버가 아닌 **읽기 복제본 또는 오프라인 덤프**를 사용합니다. 읽기 쿼리도 CPU·I/O·버퍼 풀을 사용하므로 부하를 완전히 없앨 수는 없습니다.
- 원본에는 별도 **SELECT 전용 계정**을 사용합니다. 앱은 세션을 `READ ONLY`·`READ COMMITTED`로 설정하고 값이 다르면 연결을 닫습니다.
- Source와 Target은 **서로 다른 독립 MariaDB 인스턴스**에 둡니다. 같은 서버는 schema가 달라도 차단하고, Galera가 활성화된 인스턴스도 차단하며, Target 재연결·복구·정리 때도 다시 확인합니다.
- 원본 SQL 입력창은 없으며, 메타데이터 템플릿과 앱이 생성한 인덱스 기반 조회만 실행합니다. `UPDATE`, `DELETE`, `INSERT`, `DROP`, `ALTER`, `TRUNCATE`, `FOR UPDATE`, `LOCK IN SHARE MODE`, `SLEEP()`, 파일 출력, 다중 문장은 차단합니다.
- PK 또는 NOT NULL 전체 UNIQUE 키가 있는 테이블만 전체 복사합니다. 키 없는 테이블은 **1,000행 이하 소량 테스트**만 허용합니다.
- 한 번의 Source 배치는 **최대 1,000행·약 8MiB**입니다. 조회 결과를 모두 받은 뒤 로컬 파일 처리를 시작해 서버 결과를 붙잡지 않습니다.
- 각 배치는 `EXPLAIN`으로 인덱스 접근을 확인하고, 전체 스캔·filesort·temporary 계획은 중단합니다. 데이터 조회에는 `ROWS EXAMINED 2000`을 적용합니다.
- Source 세션에는 SQL 실행 **2초**, 잠금 대기 **1초**, 네트워크 쓰기 **2초**, 유휴 연결 **30초** 제한을 적용합니다. 서버가 중단 가능한 지점에서 확인하므로 정확한 중단 시각은 보장하지 않습니다.
- 배치 사이에는 최소 **100ms** 대기하고, 느린 조회 후에는 추가로 대기합니다. 실행 전체의 원본 추출 시간 예산은 **10분**입니다.
- 같은 원본 서버에서 앱의 Source 조회는 동시에 하나만 실행합니다. 이는 서버 공통 이름의 애플리케이션 mutex이며 테이블·행 잠금은 아닙니다.
- 날짜별 비교도 등록된 모든 Source와 Target 격리를 확인하고, Target 연결을 읽기 전용 세션으로 제한합니다. `snapshot_date` 선두 인덱스를 확인하며 비교 조회에는 행·조사량 상한을 적용합니다.
- 원본 통신 오류가 나도 자동으로 전체 조회를 반복하지 않습니다. 원인을 확인한 뒤 사용자가 수동 재시도합니다.
- 상한·경고·잠금 시간 초과가 발생하면 부분 결과를 성공으로 저장하지 않고 실행을 실패 처리합니다.
- 운영 중에는 실행 시간대, CPU·디스크 I/O, buffer pool, slow query log, 복제 지연, 디스크 여유 공간을 함께 모니터링합니다. 작은 배치와 충분한 대기부터 시작합니다.
- 세션 제한은 다른 프로그램의 접근이나 계정 권한을 제거하지 않습니다. Galera·복제·프록시 토폴로지는 주소만으로 완전히 판별할 수 없으므로 운영자가 독립 인스턴스 여부를 확인해야 합니다. 원본 가용성이 최우선이면 DB 측 계정 자원 제한과 복제본을 함께 사용합니다.

제한값과 실제 잠금·느린 쿼리 검증 결과는 [원본 DB 보호](SOURCE_SAFETY.md)에 정리되어 있습니다.

## 복사·복구 규칙

- 원본 **InnoDB + PK 또는 NOT NULL 전체 UNIQUE 키**를 사용해 배치 조회합니다. 기존 스트리밍 설정도 키 배치로 처리하며, 키가 없는 테이블은 **1,000행 이하 소량 테스트만** 허용합니다.
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

스크립트가 MariaDB 11.4 **원본·대상 컨테이너 2개**를 시작하고 준비 완료를 기다린 뒤 각각의 인스턴스에 DB를 생성합니다. 원본 컨테이너에는 `snapshot_source`와 샘플 데이터만, 대상 컨테이너에는 `snapshot_target`만 생성합니다. 실행 후 두 인스턴스의 서버 식별자를 확인합니다. 재실행하면 누락된 샘플 ID만 추가하며 기존 행과 대상 데이터는 유지합니다.

준비 스크립트는 기존 schema를 삭제하지 않습니다. 테스트 데이터를 처음부터 다시 만들려면 명시적으로 `docker compose -f compose.test.yml down -v`를 실행한 뒤 준비 스크립트를 다시 실행하세요.

| 원본 테이블 | 최초 생성 행 수 | 확인할 내용 |
| --- | ---: | --- |
| `demo_customers` | 100 | 한글·이모지·NULL |
| `demo_orders` | 1,200 | 배치 분할·소수 금액·날짜·개행 |
| `demo_order_items` | 2,400 | 복합 PK |

Target에는 `demo_compare`도 생성됩니다. 날짜별 비교 메뉴에서 `2026-09-06`과 `2026-09-07`을 선택하면 item 1은 변경, item 2는 삭제, item 4는 추가된 행으로 표시됩니다.

날짜별 비교에는 두 가지 제한이 있습니다. **최대 비교 행 수**는 각 기준일에서 읽어 비교할 행 수이고, **결과 최대 건수**는 추가·삭제·변경을 합쳐 화면에 표시할 차이 행 수입니다. 동일한 행은 결과에 포함되지 않습니다. 제한에 도달하면 화면에 경고가 표시됩니다.

앱에서 원본·대상 연결을 저장하고 접속 확인 후, **복사 작업**에서 위 테이블을 선택하세요. 읽기 방식은 **PK 배치**, 배치 크기는 **100**으로 시작하고 **소량 테스트 → 전체 실행 → 결과·복구** 순서로 확인합니다. 대상 스냅샷 테이블은 앱이 생성합니다.

DBeaver 등 DB 클라이언트나 앱의 연결 설정에서 다음 정보를 사용합니다.

| 항목 | 값 |
| --- | --- |
| 호스트 | `127.0.0.1` |
| 포트 | Source: `33316` / Target: `33318` |
| 사용자 | `root` |
| 비밀번호 | `snapshot-test-only` |
| 앱 DB명 (필수) | Source: `snapshot_source` / Target: `snapshot_target` |

CLI 접속은 다음 명령을 실행하고 위 비밀번호를 입력합니다.

```sh
docker compose -f compose.test.yml exec mariadb mariadb -u root -p
```

**기존 Target 연결은 포트를 `33318`로 수정·저장하세요.** 같은 인스턴스의 `33316/snapshot_target`은 이제 차단합니다. 기존 DB는 삭제하지 않으며 새 대상에 자동 이전하지 않습니다. 대상 CLI는 위 명령의 `mariadb`를 `mariadb-target`으로 바꾸세요.

DB와 데이터는 준비 스크립트가 생성하므로 별도 SQL 입력이 필요 없습니다. 생성 SQL은 [scripts/demo_data.sql](scripts/demo_data.sql), [scripts/target_db.sql](scripts/target_db.sql)에서 확인할 수 있습니다.

**공개된 로컬 테스트 전용 계정이며 운영 환경 사용 금지입니다.** 비밀번호를 운영·공유 DB에서 재사용하거나 실제 운영 데이터·개인정보를 저장하지 마세요. [compose.test.yml](compose.test.yml)의 `127.0.0.1` 바인딩을 유지하고 외부에 포트를 공개하지 마세요.

일시 중지는 `docker compose -f compose.test.yml stop`, 컨테이너와 테스트 데이터 정리는 `docker compose -f compose.test.yml down -v`입니다.

## 개발 검증

```sh
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/ruff check .
.venv/bin/pytest -q -m 'not integration'
docker compose -f compose.test.yml up -d --wait
SNAPSHOT_TEST_PORT=33316 SNAPSHOT_TEST_TARGET_PORT=33318 SNAPSHOT_TEST_RESET=I_UNDERSTAND_DISPOSABLE_DB_RESET .venv/bin/pytest -q tests/test_integration.py
docker compose -f compose.test.yml down -v
```

**통합 테스트는 `SNAPSHOT_TEST_RESET=I_UNDERSTAND_DISPOSABLE_DB_RESET`을 명시한 경우에만 `snapshot_source`·`snapshot_target` DB를 삭제·재생성합니다. 반드시 제공된 폐기용 Docker 서버에서만 실행하세요.** 고정 비밀번호는 테스트 전용입니다. 쓰기 권한이 있는 root 연결에서도 원본 DML·DDL이 거부되는지 검증합니다.

macOS / Python 3.12 / MariaDB 11.4 검증 및 성능 기록은 [VALIDATION.md](VALIDATION.md)를 참고하세요. Python 3.10·3.12 호환성은 CI에서 검사하며, Windows 실기기 검증은 별도입니다.
