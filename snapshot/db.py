from __future__ import annotations

import datetime as dt
import re
import socket
import ssl
import time
from contextlib import contextmanager

import pymysql

MARKER = "_snapshot_runs"
PREFIX = "_snapshot_"
SOURCE_MAX_ROWS = 1000
SOURCE_MAX_BYTES = 8 * 1024 * 1024
SOURCE_WAIT_SECONDS = 0.1


VERSION_SQL = "SELECT VERSION()"
IDENTITY_SQL = "SELECT @@hostname, @@port, @@server_id, @@datadir"
WSREP_SQL = "SHOW VARIABLES LIKE 'wsrep_on'"
TABLES_SQL = (
    "SELECT TABLE_NAME FROM information_schema.TABLES "
    "WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE' "
    "ORDER BY TABLE_NAME LIMIT 1001 ROWS EXAMINED 2000"
)
ENGINE_SQL = "SELECT ENGINE FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s"
COLUMNS_SQL = "SELECT COLUMN_NAME,COLUMN_TYPE,IS_NULLABLE,CHARACTER_SET_NAME,COLLATION_NAME,EXTRA FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY ORDINAL_POSITION"
INDEXES_SQL = "SELECT INDEX_NAME,NON_UNIQUE,SEQ_IN_INDEX,COLUMN_NAME,SUB_PART FROM information_schema.STATISTICS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s ORDER BY INDEX_NAME,SEQ_IN_INDEX"
SOURCE_QUERIES = frozenset(
    (
        VERSION_SQL,
        IDENTITY_SQL,
        WSREP_SQL,
        TABLES_SQL,
        ENGINE_SQL,
        COLUMNS_SQL,
        INDEXES_SQL,
    )
)


def ident(name):
    if not isinstance(name, str) or not name or len(name) > 64 or "\x00" in name:
        raise ValueError("유효하지 않은 SQL 식별자 (1~64자)")
    return "`" + name.replace("`", "``") + "`"


def validate_profile(p):
    if p.get("role") not in ("source", "target"):
        raise ValueError("Source/Target 역할을 선택하세요.")
    for field in ("name", "host", "database", "user"):
        if not p.get(field, "").strip():
            raise ValueError(f"{field} 값이 필요합니다.")
    ident(p["database"])
    if not 1 <= int(p["port"]) <= 65535:
        raise ValueError("포트 범위 오류")


def configure_read_only(conn):
    with conn.cursor() as cur:
        cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
        cur.execute("SET SESSION TRANSACTION READ ONLY")
        cur.execute(
            "SET SESSION max_statement_time=2, lock_wait_timeout=1, "
            "innodb_lock_wait_timeout=1, net_write_timeout=2, wait_timeout=30"
        )
        cur.execute(
            "SELECT @@session.tx_read_only, @@session.tx_isolation, @@autocommit, "
            "@@max_statement_time, @@lock_wait_timeout, @@innodb_lock_wait_timeout, "
            "@@net_write_timeout, @@wait_timeout"
        )
        if cur.fetchone() != (1, "READ-COMMITTED", 1, 2, 1, 1, 2, 30):
            raise ValueError("Source 보호 설정 확인 실패")


def value_size(value):
    """Return a conservative byte estimate for a fetched cell."""
    if isinstance(value, bytes):
        return len(value)
    if isinstance(value, str):
        return len(value.encode("utf8"))
    return 32


def connect(p, secret, settings=None, target=False, read_only=False):
    validate_profile(p)
    if p["role"] != ("target" if target else "source"):
        raise ValueError("연결 역할 불일치")
    settings = settings or {}
    tls = None
    if p.get("tls"):
        tls = ssl.create_default_context(cafile=p.get("ca") or None)
        if p.get("cert"):
            tls.load_cert_chain(p["cert"], p.get("key") or None)
    options = dict(
        host=p["host"],
        port=int(p["port"]),
        database=p["database"],
        user=p["user"],
        password=secret,
        charset="utf8mb4",
        autocommit=True,
        local_infile=False,
        connect_timeout=int(settings.get("connect_timeout", 10)),
        read_timeout=int(settings.get("read_timeout", 60)),
        write_timeout=int(settings.get("write_timeout", 60)),
        binary_prefix=True,
        ssl=tls,
        ssl_disabled=not bool(tls),
    )
    if not target:
        for name in ("connect_timeout", "read_timeout", "write_timeout"):
            options[name] = min(options[name], 5)
    # UTC gives TIMESTAMP values a stable representation on both connections.
    options["init_command"] = "SET SESSION time_zone='+00:00'"
    if target:
        options["sql_mode"] = "STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION,NO_AUTO_VALUE_ON_ZERO"
    conn = pymysql.connect(**options)
    if not target or read_only:
        try:
            # tx_read_only also works on MariaDB versions before 11.1.
            configure_read_only(conn)
        except BaseException:
            conn.close()
            raise
    return conn


class ReadResult:
    """Expose fetching only, never cursor.execute or its connection."""

    def __init__(self, rows):
        self.__rows = rows
        self.__position = 0

    def fetchmany(self, size):
        result = self.__rows[self.__position : self.__position + size]
        self.__position += len(result)
        return result

    def fetchall(self):
        return self.fetchmany(len(self.__rows))


class Source:
    """Only SELECT templates can reach a source cursor. No generic write path."""

    def __init__(self, profile, secret, settings=None):
        self._conn = connect(profile, secret, settings)
        self.database = profile["database"]
        self._next_read = 0
        try:
            # Non-blocking application mutex, not a table/row lock. Released on close.
            with self._conn.cursor() as cur:
                lock_name = "_snapshot_source_reader"
                cur.execute("SELECT GET_LOCK(%s, 0)", (lock_name,))
                if cur.fetchone() != (1,):
                    raise ValueError("같은 원본 서버에서 다른 스냅샷 조회가 진행 중입니다.")
        except BaseException:
            self.close()
            raise

    @contextmanager
    def _select(self, sql, args=(), streaming=False):
        cls = pymysql.cursors.SSCursor if streaming else pymysql.cursors.Cursor
        cur = self._conn.cursor(cls)
        try:
            cur.execute(sql, args)
            # Finish the bounded server read BEFORE local file writes or caller pauses.
            data, size = [], 0
            while row := cur.fetchone():
                size += sum(value_size(value) for value in row if value is not None)
                if size > SOURCE_MAX_BYTES or len(data) >= SOURCE_MAX_ROWS:
                    raise ValueError("Source 조회 결과 상한 초과 (1,000행 / 8MiB). 배치 크기를 줄이세요.")
                data.append(row)
            if cur.warning_count:
                raise ValueError("Source 조회 경고: 부분 결과를 사용하지 않습니다.")
            yield ReadResult(data)
        except BaseException:
            if streaming:
                self.close()
            raise
        finally:
            # SSCursor.close drains unread rows. Close transport first on abort instead.
            if not self._conn.open and streaming:
                cur.connection = None
            else:
                cur.close()

    def rows(self, sql, args=()):
        if sql not in SOURCE_QUERIES:
            raise ValueError("Source는 지정된 읽기 템플릿만 허용합니다.")
        with self._select(sql, args) as result:
            return result.fetchall()

    @contextmanager
    def read(self, table, columns, key=(), last=None, limit=None):
        if type(limit) is not int or not 1 <= limit <= SOURCE_MAX_ROWS:
            raise ValueError("Source 조회는 최대 1,000행의 유한 배치만 허용합니다.")
        sql, args = read_sql(table, columns, key, last, limit)
        time.sleep(max(0, self._next_read - time.monotonic()))
        started = time.monotonic()
        try:
            if key:
                with self._select("EXPLAIN " + sql, args) as result:
                    for plan in result.fetchall():
                        if (
                            plan[3] not in ("const", "ref", "range", "index")
                            or not plan[5]
                            or any(flag in (plan[-1] or "") for flag in ("filesort", "temporary"))
                        ):
                            raise ValueError(
                                "Source 인덱스 조회 계획이 아닙니다. 전체 스캔/정렬을 차단합니다."
                            )
            with self._select(sql + " ROWS EXAMINED 2000", args, streaming=True) as result:
                elapsed = time.monotonic() - started
                self._next_read = time.monotonic() + max(SOURCE_WAIT_SECONDS, elapsed * 4)
                yield result
        finally:
            self._next_read = max(self._next_read, time.monotonic() + SOURCE_WAIT_SECONDS)

    def close(self):
        if self._conn.open:
            self._conn.close()
        # Prevent the driver's result destructor from draining a closed transport.
        result = getattr(self._conn, "_result", None)
        if result is not None:
            result.unbuffered_active = False


def rows(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        result = cur.fetchall()
        if cur.warning_count:
            raise ValueError("DB 조회 경고: 결과가 제한되었거나 부분 결과일 수 있습니다.")
        return result


def wsrep_enabled(query):
    """Return whether the connected server has active Galera replication."""
    result = query(WSREP_SQL)
    if not result:
        return False
    value = result[0][1]
    if isinstance(value, bytes):
        value = value.decode("ascii", errors="ignore")
    return str(value).casefold() in {"on", "1", "yes", "true"}


def bind_value(value):
    # PyMySQL's timedelta encoder mishandles negative fractional/multi-day TIME.
    if isinstance(value, dt.timedelta):
        micros = (value.days * 86400 + value.seconds) * 1000000 + value.microseconds
        sign = "-" if micros < 0 else ""
        seconds, fraction = divmod(abs(micros), 1000000)
        hours, rest = divmod(seconds, 3600)
        minutes, seconds = divmod(rest, 60)
        return f"{sign}{hours:02}:{minutes:02}:{seconds:02}.{fraction:06}"
    return value


class StrictCursor(pymysql.cursors.Cursor):
    def execute(self, query, args=None):
        result = super().execute(query, args)
        if self.warning_count:
            raise ValueError("적재 경고 감지: 타입 변환/잘림을 확인하세요.")
        return result


def identity(query):
    return tuple(query(IDENTITY_SQL)[0])


def assert_distinct(source_profile, target_profile, source_identity, target_identity):
    if source_identity == target_identity or (source_identity[0], source_identity[1], source_identity[3]) == (
        target_identity[0],
        target_identity[1],
        target_identity[3],
    ):
        raise ValueError(
            "Source와 Target이 같은 서버를 가리킵니다. 원본 보호를 위해 별도 MariaDB 인스턴스를 사용하세요."
        )
    try:
        a = {r[4][0] for r in socket.getaddrinfo(source_profile["host"], None)}
        b = {r[4][0] for r in socket.getaddrinfo(target_profile["host"], None)}
        if a & b and int(source_profile["port"]) == int(target_profile["port"]):
            raise ValueError("Source와 Target이 같은 서버(IP/포트)를 가리킵니다.")
    except socket.gaierror:
        pass


def check_target_isolation(conn, profile, source_profiles, get_secret):
    target_identity = identity(lambda sql: rows(conn, sql))
    if wsrep_enabled(lambda sql: rows(conn, sql)):
        raise ValueError("Galera 활성 인스턴스는 Source/Target로 사용할 수 없습니다.")
    for source_profile in source_profiles:
        src = Source(source_profile, get_secret(source_profile))
        try:
            if wsrep_enabled(src.rows):
                raise ValueError("Galera 활성 인스턴스는 Source/Target로 사용할 수 없습니다.")
            assert_distinct(source_profile, profile, identity(src.rows), target_identity)
        finally:
            src.close()


def tables(source):
    return [
        r[0]
        for r in source.rows(
            TABLES_SQL,
            (source.database,),
        )
    ]


def schema(query, database, table, *, include_indexes=False):
    ident(table)
    engine = query(
        ENGINE_SQL,
        (database, table),
    )
    if not engine:
        raise ValueError(f"테이블을 찾을 수 없습니다: {table}")
    if engine[0][0] != "InnoDB":
        raise ValueError(f"{table}: InnoDB만 지원합니다.")
    cols = query(
        COLUMNS_SQL,
        (database, table),
    )
    index_rows = query(
        INDEXES_SQL,
        (database, table),
    )
    columns = [dict(zip(("name", "type", "nullable", "charset", "collation", "extra"), r)) for r in cols]
    indexes = {}
    unique_groups = {}
    for name, non_unique, seq, col, sub in index_rows:
        indexes.setdefault(name, []).append((seq, col, sub))
        if not non_unique:
            unique_groups.setdefault(name, []).append((col, sub))
    primary = [c for c, _ in unique_groups.get("PRIMARY", [])]
    key = primary
    if not key:
        for group in unique_groups.values():
            if all(
                c and sub is None and next(x for x in columns if x["name"] == c)["nullable"] == "NO"
                for c, sub in group
            ):
                key = [c for c, _ in group]
                break
    result = {
        "columns": columns,
        "pk": primary,
        "key": key,
        "unique": {n: [c for c, _ in g] for n, g in unique_groups.items()},
    }
    if include_indexes:
        result["indexes"] = indexes
    return result


def expected_schema(source):
    if any(c["name"].casefold() == "snapshot_date" for c in source["columns"]):
        raise ValueError("원본 snapshot_date 컬럼과 충돌합니다.")
    snapshot_key = ["snapshot_date", *source["pk"]] if source["pk"] else []
    return {
        "columns": [dict(c, extra="") for c in source["columns"]]
        + [
            {
                "name": "snapshot_date",
                "type": "date",
                "nullable": "NO",
                "charset": None,
                "collation": None,
                "extra": "",
            }
        ],
        "pk": snapshot_key,
        "key": snapshot_key,
        "unique": {"PRIMARY": snapshot_key} if snapshot_key else {},
    }


def column_ddl(c):
    # COLUMN_TYPE is server metadata, never a free-form user setting.
    typ = c["type"]
    base = typ.split("(")[0].split()[0].lower()
    supported = {
        "tinyint",
        "smallint",
        "mediumint",
        "int",
        "bigint",
        "decimal",
        "float",
        "double",
        "bit",
        "char",
        "varchar",
        "binary",
        "varbinary",
        "tinytext",
        "text",
        "mediumtext",
        "longtext",
        "tinyblob",
        "blob",
        "mediumblob",
        "longblob",
        "date",
        "datetime",
        "timestamp",
        "time",
        "year",
        "enum",
        "set",
    }
    if base not in supported or "\x00" in typ:
        raise ValueError(f"지원 검토가 필요한 타입: {c['name']} ({base})")
    result = f"{ident(c['name'])} {typ}"
    if c["charset"]:
        if not re.fullmatch(r"[a-zA-Z0-9_]+", c["charset"]) or not re.fullmatch(
            r"[a-zA-Z0-9_]+", c["collation"]
        ):
            raise ValueError("문자셋 메타데이터 오류")
        result += f" CHARACTER SET {c['charset']} COLLATE {c['collation']}"
    result += " NULL" if c["nullable"] == "YES" else " NOT NULL"
    # Explicit TIMESTAMP defaults suppress legacy implicit ON UPDATE behavior.
    if base == "timestamp" and c["nullable"] == "NO":
        result += " DEFAULT '1970-01-02 00:00:00'"
    return result


def create_sql(table, expected):
    fields = [column_ddl(c) for c in expected["columns"]]
    if expected["pk"]:
        fields.append("PRIMARY KEY (" + ",".join(ident(c) for c in expected["pk"]) + ")")
    else:
        fields.append("KEY `snapshot_date_idx` (`snapshot_date`)")
    return f"CREATE TABLE {ident(table)} (" + ",".join(fields) + ") ENGINE=InnoDB"


def compare_schema(actual, expected):
    differences = []

    def clean(cols):
        return [{k: v for k, v in c.items() if k != "extra"} for c in cols]

    if clean(actual["columns"]) != clean(expected["columns"]):
        differences.append("컬럼 이름/순서/타입/NULL/문자셋이 다릅니다")
    if actual["pk"] != expected["pk"] or actual["unique"] != expected["unique"]:
        differences.append("PK/UNIQUE 키가 다릅니다")
    if any(c["extra"] for c in actual["columns"]):
        differences.append("자동 갱신/생성/AUTO_INCREMENT 컬럼이 있습니다")
    if differences:
        raise ValueError(
            "대상 스키마 불일치: " + "; ".join(differences) + "; 별도 테이블을 사용하거나 수동 조정하세요."
        )


def has_leading_index(schema_info, column):
    return any(
        any(sequence == 1 and prefix is None and indexed_column == column for sequence, indexed_column, prefix in entries)
        for entries in schema_info.get("indexes", {}).values()
    )


def prepare(conn, database, table, expected):
    def query(sql, args=()):
        return rows(conn, sql, args)

    exists = query(
        "SELECT 1 FROM information_schema.TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s", (database, table)
    )
    if not exists:
        with conn.cursor() as cur:
            cur.execute(create_sql(table, expected))
    actual = schema(query, database, table, include_indexes=True)
    compare_schema(actual, expected)
    if table != MARKER and not has_leading_index(actual, "snapshot_date"):
        raise ValueError(f"{table}: snapshot_date 선두 인덱스가 필요합니다.")
    for sql in (
        "SELECT 1 FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA=%s AND EVENT_OBJECT_TABLE=%s",
        "SELECT 1 FROM information_schema.TABLE_CONSTRAINTS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND CONSTRAINT_TYPE IN ('FOREIGN KEY','CHECK')",
        "SELECT 1 FROM information_schema.KEY_COLUMN_USAGE WHERE REFERENCED_TABLE_SCHEMA=%s AND REFERENCED_TABLE_NAME=%s",
    ):
        if query(sql, (database, table)):
            raise ValueError(f"{table}: 트리거/FK/CHECK가 있는 대상은 지원하지 않습니다.")


def keyset(key, last):
    if last is None:
        return "", []
    clauses, params = [], []
    for i, name in enumerate(key):
        clauses.append("(" + " AND ".join([f"{ident(k)}=%s" for k in key[:i]] + [f"{ident(name)}>%s"]) + ")")
        params.extend(last[:i])
        params.append(last[i])
    return " WHERE (" + " OR ".join(clauses) + ")", params


def read_sql(table, columns, key, last, limit):
    allowed = {c["name"] for c in columns}
    if (
        not columns
        or not set(key) <= allowed
        or (limit is not None and (type(limit) is not int or limit < 1))
    ):
        raise ValueError("읽기 설정 오류")
    where, args = keyset(key, last)
    sql = (
        "SELECT SQL_NO_CACHE " + ",".join(ident(c["name"]) for c in columns) + f" FROM {ident(table)}" + where
    )
    if key:
        sql += " ORDER BY " + ",".join(ident(k) for k in key)
    if limit is not None:
        sql += " LIMIT %s"
        args.append(limit)
    return sql, args


def test_connection(profile, secret, source_profiles=(), get_secret=None):
    if profile["role"] == "source":
        src = Source(profile, secret)
        try:
            if wsrep_enabled(src.rows):
                raise ValueError("Galera 활성 인스턴스는 Source/Target로 사용할 수 없습니다.")
            version = src.rows(VERSION_SQL)[0][0]
            names = tables(src)
            return f"{version} · 읽기 전용 연결 / 테이블 목록 {len(names)}개 확인", names
        finally:
            src.close()
    conn = connect(profile, secret, target=True)
    try:
        check_target_isolation(conn, profile, source_profiles, get_secret)
        version = rows(conn, VERSION_SQL)[0][0]
        # TEMPORARY objects cannot overwrite persistent source or target tables.
        with conn.cursor() as cur:
            cur.execute("CREATE TEMPORARY TABLE `_snapshot_permission_test` (n INT) ENGINE=InnoDB")
            try:
                cur.execute("INSERT INTO `_snapshot_permission_test` VALUES (1)")
                cur.execute("SELECT n FROM `_snapshot_permission_test`")
                cur.fetchall()
                cur.execute("DELETE FROM `_snapshot_permission_test`")
            finally:
                cur.execute("DROP TEMPORARY TABLE `_snapshot_permission_test`")
        return f"{version} · 임시 객체 읽기/적재/정리 확인 (영구 DDL은 실행 사전검사)", []
    finally:
        conn.close()
