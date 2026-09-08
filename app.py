from __future__ import annotations

import datetime as dt
import json
import os
import time
import uuid
from pathlib import Path

import streamlit as st

from snapshot import db, process
from snapshot.compare import available_dates, target_tables
from snapshot.compare import compare as compare_target
from snapshot.engine import build_spec, check_disk, reconcile, validate_job
from snapshot.history_ui import history_page, job_page
from snapshot.secrets import delete_secret, password, safe_error, save_secret
from snapshot.store import ACTIVE, Store
from snapshot.workflow import (
    HISTORY_PAGES,
    MENU_DESCRIPTIONS,
    MENU_FLOW,
    NAVIGATION,
    RUN_LABELS,
    STAGE_LABELS,
    STEPS,
    initial_step,
    latest_tests,
)

st.set_page_config(
    page_title="MariaDB Snapshot", page_icon="🗃️", layout="wide", initial_sidebar_state="expanded"
)
store = Store(os.environ.get("SNAPSHOT_HOME", str(Path(__file__).parent / ".snapshot")))
process.recover_local(store)
st.session_state.setdefault("secrets", {})
secrets = st.session_state.secrets
profiles = {p["id"]: p for p in store.profiles()}
jobs = {j["id"]: j for j in store.jobs()}
runs = store.runs()
sources = [p for p in profiles if profiles[p]["role"] == "source"]
targets = [p for p in profiles if profiles[p]["role"] == "target"]
active = any(r["state"] in ACTIVE or r["publish_pending"] for r in runs)
PENDING_CONFIRMATION = "_pending_confirmation"
CONFIRMED_ACTION = "_confirmed_action"


@st.dialog("실행 전 확인")
def confirmation_dialog(pending):
    st.subheader(pending["title"])
    st.info(pending["message"])
    st.caption("예상되는 동작과 결과를 확인한 뒤 계속하세요.")
    confirm, cancel = st.columns(2)
    if confirm.button("확인하고 계속", type="primary", key="confirm_action"):
        st.session_state[CONFIRMED_ACTION] = pending["action"]
        st.session_state.pop(PENDING_CONFIRMATION, None)
        st.rerun()
    if cancel.button("취소", key="cancel_action"):
        st.session_state.pop(PENDING_CONFIRMATION, None)
        st.rerun()


def confirm_button(action, label_text, title, message, container=None, **button_kwargs):
    if st.session_state.get(CONFIRMED_ACTION) == action:
        st.session_state.pop(CONFIRMED_ACTION)
        # Re-check the current page state after the modal round-trip. Another
        # run may have started while the dialog was open.
        return not button_kwargs.get("disabled", False)
    button_area = container if container is not None else st
    if button_area.button(label_text, **button_kwargs):
        st.session_state[PENDING_CONFIRMATION] = {
            "action": action,
            "title": title,
            "message": message,
        }
        st.rerun()
    return False


def go(step, message=None):
    st.session_state["_next_step"] = NAVIGATION[step - 1]
    if step in (3, 4):
        st.session_state.pop("pick_test" if step == 3 else "pick_full", None)
    if message:
        st.session_state["_notice"] = message
    st.rerun()


def report(exc):
    st.error(safe_error(exc, secrets.values()))


def label(p):
    return f"{p['name']} · {p['host']}:{p['port']}/{p['database']}"


def credentials(entries, context):
    needed = [p for p in entries if not p.get("secret_ref") and p["id"] not in secrets]
    if not needed:
        return
    st.info("저장하지 않은 비밀번호를 이번 실행에 사용할 수 있도록 입력하세요.")
    for p in needed:
        val = st.text_input(f"{p['name']} 비밀번호", type="password", key=f"credential_{context}_{p['id']}")
        if val:
            secrets[p["id"]] = val


def require_connections():
    if sources and targets:
        return
    st.info("먼저 데이터를 읽을 원본 DB와 저장할 대상 DB를 각각 등록하세요.")
    if st.button("1단계 · 연결 설정으로", type="primary"):
        go(1)
    st.stop()


def require_jobs():
    require_connections()
    if jobs:
        return
    st.info("복사할 테이블을 작업으로 등록하면 테스트와 전체 실행을 시작할 수 있습니다.")
    if st.button("2단계 · 복사 작업 만들기", type="primary"):
        go(2)
    st.stop()


def mapping(selected, current_profiles=profiles):
    return [
        {
            "순서": i + 1,
            "작업": j["name"],
            "읽을 위치": label(current_profiles[j["source_id"]]) + "/" + j["source_table"],
            "저장할 위치": label(current_profiles[j["target_id"]]) + "/" + j["target_table"],
        }
        for i, j in enumerate(selected)
    ]


if "_next_step" in st.session_state:
    st.session_state["workflow_step"] = st.session_state.pop("_next_step")
st.session_state.setdefault("workflow_step", initial_step(profiles, jobs, runs))
if pending := st.session_state.get(PENDING_CONFIRMATION):
    confirmation_dialog(pending)
st.sidebar.title("스냅샷 만들기")
page = st.sidebar.radio("진행 순서 및 조회", NAVIGATION, key="workflow_step")
st.sidebar.caption(f"원본 {len(sources)}개 · 대상 {len(targets)}개 · 복사 작업 {len(jobs)}개")
if active and page != STEPS[4]:
    st.sidebar.info("진행 중인 실행 또는 복구할 결과가 있습니다.")
    if st.sidebar.button("진행 상황 확인", type="primary"):
        go(5)
with st.sidebar.expander("비밀번호 관리"):
    st.caption("저장하지 않은 비밀번호는 이 브라우저 세션에서만 보관합니다.")
    for pid, p in profiles.items():
        val = st.text_input(p["name"], type="password", key="replace_secret_" + pid)
        if val:
            secrets[pid] = val
    if confirm_button(
        "clear_session_passwords",
        "세션 비밀번호 지우기",
        "세션 비밀번호를 지울까요?",
        "현재 브라우저 세션에 임시 보관한 비밀번호를 모두 지웁니다. DB 설정과 DB 데이터에는 영향을 주지 않습니다.",
    ):
        secrets.clear()
        for key in list(st.session_state):
            if key.startswith(("replace_secret_", "credential_")):
                del st.session_state[key]
        st.rerun()
st.title("MariaDB Snapshot")
st.caption("원본 DB의 테이블을 읽어 날짜별 분석 데이터로 저장합니다.")
st.caption("① 연결 설정 → ② 복사 작업 → ③ 소량 테스트 → ④ 전체 실행 → ⑤ 결과·복구")
st.header(page)
st.caption(MENU_DESCRIPTIONS[page])
st.info(f"**이 화면의 진행 순서:** {MENU_FLOW[page]}\n\n**화면 읽는 법:** 입력 영역은 실행할 설정, 로그는 실제 처리 과정과 오류, 출력은 검증·비교 결과입니다.")
st.divider()
if "_notice" in st.session_state:
    st.success(st.session_state.pop("_notice"))

if page == STEPS[0]:
    st.write("먼저 **어디서 읽을지**, 다음으로 **어디에 저장할지** 연결을 등록하세요.")
    c1, c2 = st.columns(2)
    c1.info(
        "① 원본 DB · "
        + (f"{len(sources)}개 등록됨" if sources else "등록 필요")
        + "\n\n연결마다 읽기 전용 모드를 강제합니다. 전용 읽기 계정을 권장합니다."
    )
    c2.info(
        "② 대상 DB · "
        + (f"{len(targets)}개 등록됨" if targets else "등록 필요")
        + "\n\n테이블 생성·적재 권한이 필요합니다."
    )
    if "_next_role" in st.session_state:
        st.session_state["connection_role"] = st.session_state.pop("_next_role")
    role = st.radio(
        "설정할 연결",
        ["source", "target"],
        horizontal=True,
        key="connection_role",
        format_func=lambda x: "① 원본 DB" if x == "source" else "② 대상 DB",
    )
    choices = sources if role == "source" else targets
    select_key = "edit_connection_" + role
    if "_select_profile" in st.session_state:
        selected_id = st.session_state.pop("_select_profile")
        if selected_id in choices:
            st.session_state[select_key] = selected_id
    selected = st.selectbox(
        "연결 선택",
        ["new", *choices],
        key=select_key,
        format_func=lambda x: "＋ 새 연결 등록" if x == "new" else label(profiles[x]),
    )
    p = profiles.get(selected, {})
    defaults = {}
    with st.form("profile_" + role + "_" + selected):
        name = st.text_input(
            "연결 이름", p.get("name", defaults.get("name", "")), placeholder="예: 운영 원본 / 로컬 분석 DB"
        )
        c1, c2 = st.columns([3, 1])
        host = c1.text_input(
            "호스트",
            p.get("host", defaults.get("host", "localhost")),
            help="DB 서버 주소입니다. 로컬 DB라면 localhost를 입력하세요.",
        )
        port = c2.number_input("포트", 1, 65535, int(p.get("port", defaults.get("port", 3306))))
        database = st.text_input("DB명", p.get("database", defaults.get("database", "")))
        user = st.text_input("사용자", p.get("user", defaults.get("user", "")))
        secret = st.text_input(
            "비밀번호",
            value=defaults.get("secret", ""),
            type="password",
            help="이미 등록한 연결은 공란이면 기존 비밀번호를 사용합니다.",
        )
        persist = st.checkbox(
            "이 PC의 안전한 자격 증명 저장소에 비밀번호 저장", value=bool(p.get("secret_ref"))
        )
        with st.expander("보안 연결 · TLS (필요한 경우)"):
            tls = st.checkbox("TLS 사용 · 인증서와 호스트 검증", value=p.get("tls", False))
            ca = st.text_input("CA 파일 경로 (공란: 시스템 CA)", p.get("ca", ""))
            cert = st.text_input("클라이언트 인증서 경로 (선택)", p.get("cert", ""))
            key = st.text_input("클라이언트 키 경로 (선택)", p.get("key", ""))
        save = st.form_submit_button("연결 저장", type="primary", disabled=active)
    if save:
        try:
            pid = p.get("id", uuid.uuid4().hex)
            data = dict(
                id=pid,
                name=name,
                role=role,
                host=host,
                port=int(port),
                database=database,
                user=user,
                tls=tls,
                ca=ca,
                cert=cert,
                key=key,
                secret_ref=pid if persist else None,
            )
            db.validate_profile(data)
            if persist and secret:
                save_secret(pid, secret)
            store.save("connection_profile", data)
            st.session_state.pop("connection_test_" + pid, None)
            if secret:
                secrets[pid] = secret
            if p.get("secret_ref") and not persist:
                delete_secret(pid)
            st.session_state["_select_profile"] = pid
            go(1, "연결을 저장했습니다. 아래에서 접속을 확인한 뒤 다음 단계로 이동하세요.")
        except Exception as exc:
            report(exc)
    if p:
        credentials([p], "connection")
        st.subheader("저장한 연결 확인")
        st.caption(label(p))
        if st.button("접속 확인", type="primary", disabled=active):
            try:
                result, names = db.test_connection(
                    p,
                    password(p, secrets),
                    [profiles[i] for i in sources],
                    lambda item: password(item, secrets),
                )
                st.session_state["connection_test_" + p["id"]] = result
                if names:
                    st.session_state["tables_" + p["id"]] = names
            except Exception as exc:
                st.session_state.pop("connection_test_" + p["id"], None)
                report(exc)
        if result := st.session_state.get("connection_test_" + p["id"]):
            st.success("접속 확인 완료 · " + result)
        if role == "source" and not targets:
            if st.button("다음 · 대상 DB 등록하기"):
                st.session_state["_next_role"] = "target"
                go(1)
        with st.expander("이 연결 삭제"):
            st.caption("사용 중인 작업이 있는 연결은 삭제할 수 없습니다.")
            if confirm_button(
                "delete_connection_" + p["id"],
                "연결 삭제",
                "이 연결을 삭제할까요?",
                "로컬 저장소의 연결 설정과 저장된 OS 자격 증명을 삭제합니다. 이 연결을 사용하는 작업이 있으면 삭제되지 않습니다.",
                disabled=active,
            ):
                try:
                    store.delete("connection_profile", p["id"])
                    if p.get("secret_ref"):
                        delete_secret(p["id"])
                    secrets.pop(p["id"], None)
                    st.session_state["_select_profile"] = "new"
                    st.session_state.pop(select_key, None)
                    go(1)
                except Exception as exc:
                    report(exc)
    if sources and targets:
        st.divider()
        st.success("원본과 대상이 준비되었습니다. 이제 복사할 테이블을 선택하세요.")
        if st.button("다음 · 복사 작업 만들기", type="primary", disabled=active):
            go(2)
    with st.expander("기존 연결 설정 가져오기 / 내보내기"):
        st.caption("비밀번호는 파일에 포함되지 않습니다. 가져온 연결은 비밀번호를 다시 입력하세요.")
        st.download_button(
            "연결 설정 내보내기",
            json.dumps(list(profiles.values()), ensure_ascii=False, indent=2),
            "connections.json",
            "application/json",
        )
        uploaded = st.file_uploader("연결 설정 파일 선택", type="json")
        if uploaded:
            if confirm_button(
                "import_connections",
                "연결 설정 가져오기",
                "연결 설정을 가져올까요?",
                "파일의 연결 설정을 로컬 저장소에 추가하거나 같은 ID의 기존 설정으로 덮어씁니다. 비밀번호는 가져오지 않으며, 현재 DB에는 접속하지 않습니다.",
                disabled=active,
            ):
                try:
                    allowed = {"name", "role", "host", "port", "database", "user", "tls", "ca", "cert", "key"}
                    clean = [{k: v for k, v in entry.items() if k in allowed} for entry in json.load(uploaded)]
                    for entry in clean:
                        db.validate_profile(entry)
                    for entry in clean:
                        store.save("connection_profile", entry)
                    go(1, "연결 설정을 가져왔습니다. 비밀번호를 입력하고 접속을 확인하세요.")
                except Exception as exc:
                    report(exc)

elif page == STEPS[1]:
    require_connections()
    st.write(
        "**원본 테이블 하나 → 대상 테이블 하나**를 복사 작업으로 저장하세요. 여러 테이블은 작업을 각각 추가합니다."
    )
    if "_edit_job" in st.session_state:
        st.session_state["job_editor"] = st.session_state.pop("_edit_job")
    if st.session_state.get("job_editor") not in ["new", *jobs]:
        st.session_state["job_editor"] = "new"
    selected = st.selectbox(
        "작업 선택",
        ["new", *jobs],
        key="job_editor",
        format_func=lambda x: "＋ 새 복사 작업" if x == "new" else jobs[x]["name"],
    )
    j = jobs.get(selected, {})
    source_id = st.selectbox(
        "① 읽을 원본 DB",
        sources,
        index=sources.index(j["source_id"]) if j else 0,
        format_func=lambda x: label(profiles[x]),
        disabled=active,
    )
    credentials([profiles[source_id]], "table_list")
    if st.button("원본 테이블 목록 가져오기", disabled=active):
        src = None
        try:
            src = db.Source(profiles[source_id], password(profiles[source_id], secrets))
            st.session_state["tables_" + source_id] = db.tables(src)
        except Exception as exc:
            report(exc)
        finally:
            if src:
                src.close()
    names = st.session_state.get("tables_" + source_id, [])
    if not names:
        st.caption("목록을 가져오거나 알고 있는 테이블 이름을 직접 입력하세요.")
    with st.form("job_" + selected + "_" + source_id):
        if names:
            source_table = st.selectbox(
                "② 원본 테이블",
                names,
                index=names.index(j["source_table"]) if j.get("source_table") in names else 0,
            )
        else:
            source_table = st.text_input("② 원본 테이블", j.get("source_table", ""))
        target_id = st.selectbox(
            "③ 저장할 대상 DB",
            targets,
            index=targets.index(j["target_id"]) if j else 0,
            format_func=lambda x: label(profiles[x]),
        )
        target_table = st.text_input(
            "④ 대상 테이블 이름", j.get("target_table", ""), placeholder="공란이면 원본 테이블과 같은 이름"
        )
        name = st.text_input(
            "작업 이름 (선택)", j.get("name", ""), placeholder="공란이면 테이블 이름을 사용합니다"
        )
        with st.expander("원본 보호와 부하 조절 · 최대 1,000행 / 최소 100ms"):
            mode = "pk"
            st.caption(
                "PK 또는 NOT NULL 유일 키로 나누어 읽습니다. 키 없는 테이블은 1,000행 이하 소량 테스트만 허용합니다."
            )
            cols = st.columns(2)
            batch = cols[0].number_input("배치 행 수", 1, 1000, min(int(j.get("batch_rows", 1000)), 1000))
            wait = cols[1].number_input(
                "배치 사이 대기 (ms)", 100, 60000, max(int(j.get("wait_ms", 300)), 100)
            )
            st.caption(
                "원본 SQL 최대 2초, 잠금 대기 1초, 배치 결과 8MiB. 상한 초과 시 부분 결과를 버리고 중단합니다."
            )
        with st.expander("연결 제한 시간과 재시도"):
            cols = st.columns(4)
            ct = cols[0].number_input("연결 제한 (초)", 1, 86400, int(j.get("connect_timeout", 10)))
            rt = cols[1].number_input("읽기 제한 (초)", 1, 86400, int(j.get("read_timeout", 60)))
            wt = cols[2].number_input("쓰기 제한 (초)", 1, 86400, int(j.get("write_timeout", 60)))
            retries = cols[3].number_input("대상 통신 재시도 횟수", 0, 5, int(j.get("retries", 2)))
        save = st.form_submit_button("복사 작업 저장", type="primary", disabled=active)
    if save:
        try:
            data = dict(
                name=name or source_table,
                source_id=source_id,
                target_id=target_id,
                source_table=source_table,
                target_table=target_table or source_table,
                read_mode=mode,
                batch_rows=int(batch),
                wait_ms=int(wait),
                connect_timeout=int(ct),
                read_timeout=int(rt),
                write_timeout=int(wt),
                retries=int(retries),
            )
            if j:
                data["id"] = j["id"]
            validate_job(data)
            jid = store.save("backup_job", data)
            picked = st.session_state.get("chosen_jobs", [])
            st.session_state["chosen_jobs"] = list(dict.fromkeys([*picked, jid]))
            go(2, "작업을 저장했습니다. 작업을 더 추가하거나 아래에서 소량 테스트로 이동하세요.")
        except Exception as exc:
            report(exc)
    if j:
        with st.expander("이 작업 복제 / 삭제"):
            c1, c2 = st.columns(2)
            if c1.button("작업 복제", disabled=active):
                try:
                    store.save("backup_job", dict(j, id=uuid.uuid4().hex, name=j["name"] + " 복사"))
                    go(2, "작업을 복제했습니다.")
                except Exception as exc:
                    report(exc)
            if confirm_button(
                "delete_job_" + j["id"],
                "작업 삭제",
                "이 복사 작업을 삭제할까요?",
                "로컬 작업 설정만 삭제합니다. 이미 실행한 이력과 결과는 보존되며, DB 스냅샷은 변경하지 않습니다.",
                container=c2,
                disabled=active,
            ):
                try:
                    store.delete("backup_job", j["id"])
                    go(2, "작업을 삭제했습니다.")
                except Exception as exc:
                    report(exc)
        with st.expander("이 작업의 실행 이력"):
            history = []
            for r in runs:
                for item in store.tables(r["id"]):
                    if item["job_id"] == j["id"]:
                        history.append(
                            {
                                "기준일": json.loads(r["spec"])["date"],
                                "결과": RUN_LABELS[r["state"]],
                                "행 수": item["extracted"],
                                "오류": item["error"],
                                "실행 ID": r["id"],
                            }
                        )
            st.dataframe(history, hide_index=True)
    if jobs:
        st.divider()
        st.subheader(f"준비된 복사 작업 · {len(jobs)}개")
        st.dataframe(mapping(list(jobs.values())), hide_index=True, use_container_width=True)
        if st.button("다음 · 소량 테스트", type="primary", disabled=active):
            go(3)

elif page in STEPS[2:4]:
    require_jobs()
    is_test = page == STEPS[2]
    st.write(
        "먼저 적은 행으로 **읽기 → 임시 적재 → 검증**이 되는지 확인합니다. 정식 스냅샷은 변경하지 않습니다."
        if is_test
        else "선택한 테이블 전체를 읽고 검증한 뒤 **기준일의 스냅샷을 한 번에 저장**합니다."
    )
    default = [i for i in st.session_state.get("chosen_jobs", list(jobs)) if i in jobs]
    selected = st.multiselect(
        "실행할 작업 · 선택한 순서대로 처리합니다",
        list(jobs),
        default=default,
        format_func=lambda x: jobs[x]["name"],
        disabled=active,
        key="pick_test" if is_test else "pick_full",
    )
    st.session_state["chosen_jobs"] = selected
    if selected:
        st.dataframe(mapping([jobs[i] for i in selected]), hide_index=True, use_container_width=True)
    limit = None
    if is_test:
        range_name = st.radio(
            "작업마다 읽을 행 수", ["100행 (권장 시작)", "1,000행", "직접 입력"], horizontal=True
        )
        limit = 100 if range_name == "100행 (권장 시작)" else 1000
        if range_name == "직접 입력":
            limit = int(st.number_input("테스트 행 수", 1, 100000000, 100))
        st.caption("소량 테스트 성공이 전체 데이터의 성능이나 모든 타입 호환성을 보장하지는 않습니다.")
    else:
        tests = latest_tests(jobs, profiles, runs)
        untested = [jobs[i]["name"] for i in selected if i not in tests or tests[i]["state"] != "SUCCESS"]
        if untested:
            st.warning("현재 설정으로 성공한 소량 테스트가 없는 작업: " + ", ".join(untested))
            if st.button("소량 테스트 먼저 하기"):
                go(3)
        elif selected:
            st.success("선택한 모든 작업이 현재 설정으로 소량 테스트를 통과했습니다.")
    date = st.date_input(
        "기준일",
        st.session_state.get("chosen_date", dt.date.today()),
        help="데이터를 구분할 날짜입니다. 수집한 날짜 또는 업무 기준 날짜를 선택하세요.",
    )
    st.session_state["chosen_date"] = date
    if not is_test:
        st.info(
            f"{date}에 이미 저장한 데이터가 있으면 해당 날짜 전체를 교체합니다. 다른 날짜의 데이터는 유지합니다."
        )
        st.caption("복사 도중 원본이 변경되면 배치별 읽기 시점이 달라집니다. 원본 갱신이 끝난 뒤 실행하세요.")
    with st.expander("중간 파일 저장 위치"):
        work_dir = st.text_input("저장 폴더", st.session_state.get("work_dir", str(store.root / "files")))
        st.session_state["work_dir"] = work_dir
        st.caption(
            "원본에서 읽은 데이터를 잠시 보관합니다. 실행 전 쓰기 가능 여부와 여유 공간을 자동 확인합니다."
        )
        if st.button("폴더와 여유 공간 확인"):
            try:
                _, free = check_disk(work_dir)
                st.success(f"쓰기 가능 · 여유 {free / 1024**3:.2f} GiB")
            except Exception as exc:
                report(exc)
    # All registered Sources participate in Target isolation checks, including
    # Sources not selected for this run.
    needed = set(sources) | {jobs[i]["target_id"] for i in selected}
    credentials([profiles[pid] for pid in sorted(needed)], "launch")
    incompatible = len({jobs[i]["target_id"] for i in selected}) > 1
    if incompatible:
        st.error("한 번에 실행할 작업은 같은 대상 DB 연결을 사용해야 합니다. 작업 선택을 조정하세요.")
    st.divider()
    if selected:
        st.write(
            f"**실행 요약:** {len(selected)}개 작업 · {date} · "
            + (f"작업당 최대 {limit:,}행 테스트" if is_test else "전체 행 저장")
        )
    button = f"{len(selected)}개 작업 테스트 시작" if is_test else f"{len(selected)}개 작업 전체 실행"
    action = "start_test" if is_test else "start_full"
    title = "소량 테스트를 시작할까요?" if is_test else "전체 스냅샷을 실행할까요?"
    message = (
        f"선택한 {len(selected)}개 작업에서 원본을 최대 {limit:,}행씩 읽고 대상 DB의 임시 테이블에 적재·검증합니다. "
        "정식 스냅샷은 변경하지 않지만 대상 DB와 로컬 중간 파일을 사용합니다."
        if is_test
        else f"선택한 {len(selected)}개 작업의 원본 전체 데이터를 읽어 대상 DB에 저장합니다. 기준일 {date}의 기존 행은 교체되고 다른 기준일은 유지됩니다."
    )
    if confirm_button(
        action,
        button,
        title,
        message,
        type="primary",
        disabled=active or not selected or incompatible,
    ):
        try:
            spec = build_spec(store, selected, date, limit, work_dir)
            st.session_state["view_run"] = process.start(store, spec, secrets)
            go(5)
        except Exception as exc:
            report(exc)

elif page == HISTORY_PAGES[0]:
    job_page(store, go, active)

elif page == HISTORY_PAGES[1]:
    history_page(store, go)

elif page == HISTORY_PAGES[2]:
    require_connections()
    st.write("대상 DB의 같은 테이블을 두 기준일로 비교합니다. 원본 DB에는 연결하지 않습니다.")
    target_ids = targets
    target_id = st.selectbox("비교할 대상 DB", target_ids, format_func=lambda x: label(profiles[x]))
    target_profile = profiles[target_id]
    compare_sources = [profiles[pid] for pid in sources]
    credentials([target_profile, *compare_sources], "compare")
    conn = None
    try:
        # Check server identity before selecting the target schema. This keeps
        # an aliased Source/Target endpoint from failing with a misleading
        # unknown-database error before the isolation guard runs.
        conn = db.connect(
            target_profile,
            password(target_profile, secrets),
            target=True,
            read_only=True,
            select_database=False,
        )
        db.check_target_isolation(conn, target_profile, compare_sources, lambda p: password(p, secrets))
        conn.select_db(target_profile["database"])
        names = target_tables(conn, target_profile["database"])
        if not names:
            st.info("비교할 대상 스냅샷 테이블이 없습니다. 먼저 전체 실행을 완료하세요.")
            st.stop()
        table = st.selectbox("대상 테이블", names)
        dates = available_dates(conn, target_profile["database"], table)
        if len(dates) < 2:
            st.info("비교하려면 같은 테이블에 서로 다른 기준일의 스냅샷이 두 개 이상 필요합니다.")
            st.stop()
        older = st.selectbox("이전 기준일", dates, format_func=str)
        newer = st.selectbox("비교 기준일", [date for date in dates if date != older], format_func=str)
        max_rows = st.number_input("날짜별 최대 비교 행 수", 1, 5000, 5000, step=100)
        max_results = st.number_input("결과 최대 건수", 1, 1000, 200, step=50)
        if st.button("행 비교", type="primary"):
            result = compare_target(
                conn, target_profile["database"], table, older, newer, int(max_rows), int(max_results)
            )
            st.session_state["compare_result"] = result
        result = st.session_state.get("compare_result")
        if (
            result
            and result["table"] == table
            and result["older"] == older
            and result["newer"] == newer
            and result.get("max_rows") == int(max_rows)
            and result.get("max_results") == int(max_results)
        ):
            if result["truncated"]:
                st.warning("비교 행 수 상한에 도달했습니다. 전체 차이가 아니라 상한 내 결과입니다.")
            if result["results_truncated"]:
                st.warning("결과 최대 건수에 도달했습니다. 표시 결과는 설정한 건수까지입니다.")
            st.dataframe(
                {
                    "구분": ["이전 행 수", "비교 행 수", "추가", "삭제", "변경"],
                    "값": [
                        result["older_count"],
                        result["newer_count"],
                        len(result["added"]),
                        len(result["removed"]),
                        len(result["changed"]),
                    ],
                },
                hide_index=True,
                use_container_width=True,
            )
            changes = []
            changes.extend(
                {
                    "구분": "추가",
                    "키": repr(item["key"]),
                    "행 데이터": json.dumps(item["row"], ensure_ascii=False, default=str),
                    "변경 컬럼": "—",
                }
                for item in result["added"]
            )
            changes.extend(
                {
                    "구분": "삭제",
                    "키": repr(item["key"]),
                    "행 데이터": json.dumps(item["row"], ensure_ascii=False, default=str),
                    "변경 컬럼": "—",
                }
                for item in result["removed"]
            )
            changes.extend(
                {
                    "구분": "변경",
                    "키": repr(item["key"]),
                    "행 데이터": "—",
                    "변경 컬럼": ", ".join(item["columns"]),
                }
                for item in result["changed"]
            )
            st.subheader("차이 샘플")
            st.dataframe(changes, hide_index=True, use_container_width=True)
    except Exception as exc:
        report(exc)
    finally:
        if conn:
            conn.close()

else:
    if not runs:
        st.info("아직 실행한 기록이 없습니다. 연결과 복사 작업을 준비한 뒤 소량 테스트를 시작하세요.")
        next_step = initial_step(profiles, jobs, runs)
        if st.button("다음 · " + next_step, type="primary"):
            go(STEPS.index(next_step) + 1)
        st.stop()
    ids = [r["id"] for r in runs]
    requested = st.session_state.get("view_run")
    if requested and requested not in ids:
        # History browsing may open any persisted run, including one older than the recent 100.
        try:
            store.run(requested)
            ids.insert(0, requested)
        except ValueError:
            st.session_state.pop("view_run", None)
    view = st.selectbox(
        "확인할 실행",
        ids,
        index=ids.index(st.session_state.get("view_run")) if st.session_state.get("view_run") in ids else 0,
        format_func=lambda x: (
            f"{store.run(x)['spec']['date']} · {'테스트' if store.run(x)['spec']['limit'] else '전체 실행'} · {RUN_LABELS[store.run(x)['state']]} · {x[:8]}"
        ),
    )

    @st.fragment(run_every=1)
    def monitor(run_id):
        process.recover_local(store)
        r = store.run(run_id)
        if any(row["state"] in ACTIVE or row["publish_pending"] for row in store.runs()) != active:
            st.rerun()
        spec = r["spec"]
        items = store.tables(run_id)
        is_test = spec["limit"] is not None
        st.subheader(
            f"{RUN_LABELS[r['state']]} · {'소량 테스트' if is_test else '전체 스냅샷'} · {spec['date']}"
        )
        st.caption(" → ".join(j["name"] for j in spec["jobs"]))
        if r["state"] == "SUCCESS":
            st.success(
                "테스트가 완료되었습니다. 정식 데이터는 변경하지 않았습니다. 같은 작업으로 전체 실행을 진행할 수 있습니다."
                if is_test
                else "선택한 모든 테이블의 검증과 최종 저장이 완료되었습니다."
            )
            if st.button(
                "다음 · 같은 작업 전체 실행" if is_test else "새 스냅샷 실행",
                type="primary",
                key="next_" + run_id,
                disabled=active,
            ):
                st.session_state["chosen_jobs"] = [j["id"] for j in spec["jobs"] if j["id"] in jobs]
                st.session_state["chosen_date"] = dt.date.fromisoformat(spec["date"])
                st.session_state.pop("pick_full", None)
                go(4)
        elif r["publish_pending"]:
            st.warning(
                "최종 저장 결과를 확인해야 합니다. 아래의 결과 확인을 먼저 완료하면 새 실행을 시작할 수 있습니다."
            )
        elif r["state"] in ("FAILED", "CANCELLED", "INTERRUPTED"):
            st.info(
                "복사가 끝나지 않았습니다. 완료된 중간 파일을 활용해 다시 시도하거나 작업 설정을 수정하세요."
            )
        else:
            st.info(
                "화면을 닫아도 복사는 계속됩니다. 이 화면으로 돌아오면 진행 상황을 다시 확인할 수 있습니다."
            )
        cols = st.columns(3)
        cols[0].metric(
            "검증 완료", f"{sum(i['stage'] in ('READY', 'PUBLISHED') for i in items)} / {len(items)}개"
        )
        cols[1].metric("경과 시간", f"{((r['ended'] or time.time()) - (r['started'] or r['created'])):.0f}초")
        cols[2].metric("읽은 행", f"{sum(i['extracted'] for i in items):,}")
        st.dataframe(
            [
                {
                    "작업": j["name"],
                    "현재 단계": STAGE_LABELS[i["stage"]],
                    "읽은 행": i["extracted"],
                    "임시 적재 행": i["loaded"],
                    "임시 적재율": f"{i['loaded'] / i['extracted']:.0%}"
                    if i["extracted"]
                    else ("100% · 빈 테이블" if i["stage"] in ("READY", "PUBLISHED") else "—"),
                    "오류": i["error"],
                }
                for i, j in zip(items, spec["jobs"])
            ],
            hide_index=True,
            use_container_width=True,
        )
        st.caption(
            "임시 적재 100%는 최종 완료가 아닙니다. 모든 테이블의 검증과 최종 저장까지 끝나야 완료로 표시됩니다."
        )
        if r["error"]:
            st.error(r["error"])
        if r["state"] in ACTIVE:
            if confirm_button(
                "cancel_" + run_id,
                "복사 취소",
                "복사를 취소할까요?",
                "취소 요청을 보내고 후속 테이블 처리를 중단합니다. 이미 진행 중인 DB 요청은 끝날 때까지 지연될 수 있으며, 중간 결과는 복구 화면에서 확인합니다.",
                key="cancel_" + run_id,
                disabled=r["state"] == "CANCEL_REQUESTED",
            ):
                store.cancel(run_id)
                st.rerun(scope="fragment")
            st.caption("진행 중인 DB 요청이 끝날 때까지 취소가 지연될 수 있습니다.")
            with st.expander("취소가 오래 걸릴 때 · 강제 중단"):
                st.warning("이 복사 프로세스를 강제 종료합니다. 서버 쿼리의 즉시 종료는 보장하지 않습니다.")
                confirm = st.checkbox(
                    "강제 종료 후 복구가 필요할 수 있음을 확인했습니다", key="force_confirm_" + run_id
                )
                if confirm_button(
                    "force_" + run_id,
                    "강제 중단",
                    "복사 프로세스를 강제 중단할까요?",
                    "worker 프로세스를 종료합니다. 서버의 현재 쿼리가 즉시 멈춘다는 보장은 없고, 대상 staging과 공개 상태를 결과·복구 화면에서 확인해야 할 수 있습니다.",
                    key="force_" + run_id,
                    disabled=not confirm,
                ):
                    try:
                        process.force_stop(store, run_id)
                        st.rerun(scope="fragment")
                    except Exception as exc:
                        report(exc)
        if r["state"] in ("FAILED", "CANCELLED", "INTERRUPTED") or r["publish_pending"]:
            credentials(list(spec["profiles"].values()), "recovery_" + run_id)
            if r["publish_pending"]:
                if st.button(
                    "1. 최종 저장 결과 확인",
                    type="primary",
                    key="recover_" + run_id,
                    disabled=process.owned_process(r) is not None,
                ):
                    try:
                        reconcile(store, run_id, secrets)
                        st.rerun()
                    except Exception as exc:
                        report(exc)
            else:
                c1, c2 = st.columns(2)
                if confirm_button(
                    "retry_" + run_id,
                    "같은 설정으로 다시 시도",
                    "같은 설정으로 다시 시도할까요?",
                    "검증된 중간 파일은 재사용할 수 있지만 대상 staging 적재와 검증을 다시 수행합니다. 전체 실행이면 같은 기준일의 대상 데이터가 다시 교체될 수 있습니다.",
                    type="primary",
                    key="retry_" + run_id,
                    disabled=active,
                ):
                    try:
                        st.session_state["view_run"] = process.retry(store, run_id, secrets)
                        st.rerun()
                    except Exception as exc:
                        report(exc)
                if c2.button("작업 설정 수정", key="edit_" + run_id, disabled=active):
                    go(2)
        with st.expander("실행 설정과 연결 상태"):
            st.dataframe(mapping(spec["jobs"], spec["profiles"]), hide_index=True, use_container_width=True)
            st.write(r["server_status"])
            st.caption(
                f"실행 ID: {run_id} · 기준일 {spec['date']} · "
                + (f"작업당 최대 {spec['limit']}행" if is_test else "전체 행")
            )
            for j in spec["jobs"]:
                st.caption(
                    f"{j['name']}: {j['batch_rows']:,}행 / {j['wait_ms']}ms · "
                    + ("키 기준 배치" if j["read_mode"] == "pk" else "키 배치 / 키 없으면 소량 테스트만")
                )
        with st.expander("상세 진단 · 속도, 연결, 생존 신호"):
            st.write("복사 프로세스: " + ("생존" if process.owned_process(r) else "종료 / 시작 대기"))
            st.write(
                f"최근 생존 신호: {time.time() - r['heartbeat']:.0f}초 전"
                if r["heartbeat"]
                else "생존 신호 대기 중"
            )
            st.caption(
                "진행 지연만으로 실패를 판정하지 않습니다. 표시를 위해 원본 DB를 반복 조회하지 않습니다."
            )

            def stamp(value):
                return dt.datetime.fromtimestamp(value).isoformat(timespec="seconds") if value else "—"

            st.dataframe(
                [
                    {
                        "작업": j["name"],
                        "연결": i["connection_status"],
                        "읽은 MiB": round(i["bytes"] / 1024**2, 2),
                        "평균 MiB/s": round(
                            i["bytes"]
                            / 1024**2
                            / max(0.001, (i["ended"] or time.time()) - (i["started"] or time.time())),
                            2,
                        ),
                        "읽기 시작": stamp(i["read_started"]),
                        "읽기 종료": stamp(i["read_ended"]),
                        "최근 데이터 진행": stamp(i["progress"]),
                    }
                    for i, j in zip(items, spec["jobs"])
                ],
                hide_index=True,
            )
        with st.expander("로그 확인 / 저장", expanded=bool(r["error"])):
            log_path = Path(spec["work_dir"]) / run_id / "events.jsonl"
            if log_path.exists():
                with log_path.open("rb") as fp:
                    fp.seek(0, 2)
                    size = fp.tell()
                    fp.seek(max(0, size - 65536))
                    if size > 65536:
                        fp.readline()
                    tail = fp.read().decode("utf8", errors="replace")
                errors_only = st.checkbox("오류만 표시", key="log_filter_" + run_id)
                lines = [line for line in tail.splitlines() if not errors_only or '"ERROR"' in line]
                st.code("\n".join(lines[-150:]) or "표시할 로그 없음", language=None)
                st.download_button(
                    "표시 로그 저장", "\n".join(lines), f"{run_id}.log", key="download_" + run_id
                )
                st.caption("최근 최대 64 KiB를 표시합니다.")
            else:
                st.caption("아직 기록된 로그가 없습니다.")
        if r["state"] not in ACTIVE:
            with st.expander("중간 파일 정리"):
                st.caption(
                    "이 실행의 임시 적재 테이블과 중간 파일만 지웁니다. 정식 스냅샷과 로그는 유지합니다. 정리 후에는 원본부터 다시 읽습니다."
                )
                credentials(list(spec["profiles"].values()), "cleanup_" + run_id)
                if confirm_button(
                    "clean_" + run_id,
                    "이 실행의 중간 파일 정리",
                    "이 실행의 중간 파일을 정리할까요?",
                    "이 실행의 staging 테이블과 중간 파일을 삭제합니다. 정식 스냅샷과 로그는 유지되지만, 이후 재시도는 원본에서 다시 읽어야 합니다.",
                    key="clean_" + run_id,
                    disabled=active or bool(r["publish_pending"]),
                ):
                    try:
                        process.cleanup(store, run_id, secrets)
                        st.success("중간 파일 정리를 완료했습니다.")
                    except Exception as exc:
                        report(exc)

    monitor(view)
