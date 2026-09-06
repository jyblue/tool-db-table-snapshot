"""Read-only job/run browsing; all queries use local SQLite."""

import csv
import datetime as dt
import io
import json
import math
import time

import streamlit as st

from .workflow import RUN_LABELS, STAGE_LABELS

ACTIONS = {
    "JOB_CREATED": "작업 생성",
    "JOB_UPDATED": "작업 수정",
    "JOB_DELETED": "작업 삭제",
    "RUN_REQUESTED": "실행 요청",
    "RETRY_REQUESTED": "재시도 요청",
    "WORKER_STARTED": "실행 프로세스 시작",
    "STATE_CHANGED": "실행 상태 변경",
    "CANCEL_REQUESTED": "취소 요청",
    "FORCE_STOP_REQUESTED": "강제 중단 요청",
    "PROCESS_INTERRUPTED": "프로세스 중단 감지",
    "FILES_CLEANED": "중간 파일 정리",
}


def stamp(value):
    return dt.datetime.fromtimestamp(value).isoformat(sep=" ", timespec="seconds") if value else "—"


def activity_rows(events):
    return [
        {
            "시각": stamp(e["occurred"]),
            "구분": "사용자 요청" if e["actor"] == "user" else "시스템",
            "내역": ACTIONS.get(e["action"], e["action"]),
            "작업": e["name"] or "—",
            "설명": e["detail"] or "—",
            "실행 ID": e["run_id"] or "—",
        }
        for e in events
    ]


def csv_bytes(records):
    out = io.StringIO(newline="")
    if records:
        writer = csv.DictWriter(out, fieldnames=list(records[0]))
        writer.writeheader()
        # Avoid spreadsheet formula execution when names came from DB/user input.
        writer.writerows(
            {
                k: ("'" + v if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")) else v)
                for k, v in row.items()
            }
            for row in records
        )
    return out.getvalue().encode("utf-8-sig")


def page_offset(total, key, size=50):
    pages = max(1, math.ceil(total / size))
    if st.session_state.get(key, 1) > pages:
        st.session_state[key] = 1
    page = st.number_input("페이지", 1, pages, key=key)
    st.caption(f"총 {total:,}건 · {page}/{pages}페이지 · 페이지당 {size}건")
    return (page - 1) * size


def job_page(store, go, active):
    st.write(
        "등록한 복사 작업과 작업별 최근 실행 결과를 조회합니다. 작업을 선택해도 복사를 시작하지 않습니다."
    )
    keyword = st.text_input("작업 검색 · 이름 또는 테이블")
    include_deleted = st.checkbox("삭제된 작업의 실행 기록도 포함")
    catalog = store.job_overview()
    visible = [
        item
        for item in catalog
        if (include_deleted or not item["deleted"])
        and (
            not keyword
            or keyword.casefold()
            in " ".join(
                str(item["job"].get(k, "")) for k in ("name", "source_table", "target_table")
            ).casefold()
        )
    ]
    if not visible:
        st.info("조회할 작업이 없습니다. 검색 조건을 바꾸거나 복사 작업을 등록하세요.")
        if st.button("복사 작업 만들기", type="primary", disabled=active):
            go(2)
        return
    st.dataframe(
        [
            {
                "작업": i["job"]["name"],
                "원본 테이블": i["job"]["source_table"],
                "대상 테이블": i["job"]["target_table"],
                "등록 상태": "삭제됨" if i["deleted"] else "등록됨",
                "실행 횟수": i["count"],
                "최근 실행": stamp(i["last_created"]),
                "최근 결과": RUN_LABELS.get(i["last_state"], "미실행"),
            }
            for i in visible
        ],
        hide_index=True,
        use_container_width=True,
    )
    options = {i["job"]["id"]: i for i in visible}
    jid = st.selectbox("상세를 볼 작업", list(options), format_func=lambda x: options[x]["job"]["name"])
    item = options[jid]
    job = item["job"]
    st.subheader(job["name"])
    for role, table, label in (("source_id", "source_table", "원본"), ("target_id", "target_table", "대상")):
        p = item["profiles"].get(job[role], {})
        st.write(
            f"**{label}:** {p.get('host', '—')}:{p.get('port', '—')}/{p.get('database', '—')}/{job[table]}"
        )
    st.write(
        f"**읽기:** {'키 기준 배치' if job['read_mode'] == 'pk' else '단일 스트리밍'} · {job['batch_rows']:,}행 · {job['wait_ms']}ms 대기"
    )
    c1, c2 = st.columns(2)
    if c1.button("이 작업의 실행 이력", type="primary"):
        st.session_state["_history_job"] = jid
        go(7)
    if not item["deleted"] and c2.button("작업 설정 열기", disabled=active):
        st.session_state["_edit_job"] = jid
        go(2)
    if item["deleted"]:
        st.caption("작업은 삭제되었지만 당시 실행 설정과 결과는 보존되어 있습니다.")


def run_rows(records):
    output = []
    for r in records:
        spec = json.loads(r["spec"])
        duration = (r["ended"] or time.time()) - (r["started"] or r["created"])
        output.append(
            {
                "요청 시각": stamp(r["created"]),
                "시작 시각": stamp(r["started"]),
                "종료 시각": stamp(r["ended"]),
                "소요 초": round(max(0, duration), 1),
                "기준일": spec["date"],
                "종류": "전체" if spec["limit"] is None else f"테스트 {spec['limit']}행",
                "작업": " → ".join(j["name"] for j in spec["jobs"]),
                "결과": RUN_LABELS[r["state"]] + (" · 공개 확인 필요" if r["publish_pending"] else ""),
                "테이블 수": r["table_count"],
                "추출 행": r["extracted"],
                "임시 적재 행": r["loaded"],
                "오류": r["error"] or "",
                "재시도 원본": r["retry_of"] or "",
                "실행 ID": r["id"],
            }
        )
    return output


def history_page(store, go):
    st.write(
        "실행 요청은 자동 기록됩니다. 작업 수정·삭제나 앱 재시작 후에도 당시의 설정과 결과를 조회할 수 있습니다."
    )
    executions, actions = st.tabs(["실행 목록", "사용자 작업 내역"])
    with executions:
        catalog = {i["job"]["id"]: i for i in store.job_overview()}
        if "_history_job" in st.session_state:
            st.session_state["history_job"] = st.session_state.pop("_history_job")
        if st.session_state.get("history_job") not in [None, *catalog]:
            st.session_state["history_job"] = None
        job = st.selectbox(
            "작업으로 조회",
            [None, *catalog],
            key="history_job",
            format_func=lambda x: (
                "전체 작업"
                if x is None
                else catalog[x]["job"]["name"] + (" (삭제됨)" if catalog[x]["deleted"] else "")
            ),
        )
        keyword = st.text_input("실행 검색 · 작업명, 테이블명, 실행 ID")
        c1, c2 = st.columns(2)
        state = c1.selectbox(
            "실행 결과",
            [None, *RUN_LABELS],
            format_func=lambda x: "모든 결과" if x is None else RUN_LABELS[x],
        )
        mode = c2.selectbox(
            "실행 종류",
            [None, "test", "full"],
            format_func=lambda x: {None: "테스트 + 전체", "test": "테스트", "full": "전체"}[x],
        )
        after = before = None
        if st.checkbox("실행 요청일로 기간 지정"):
            c1, c2 = st.columns(2)
            first = c1.date_input("조회 시작일", dt.date.today() - dt.timedelta(days=30))
            last = c2.date_input("조회 종료일", dt.date.today())
            if first > last:
                st.error("조회 종료일은 시작일 이후여야 합니다.")
                return
            after = dt.datetime.combine(first, dt.time.min).timestamp()
            before = dt.datetime.combine(last + dt.timedelta(days=1), dt.time.min).timestamp()
        filters = dict(job_id=job, keyword=keyword, state=state, mode=mode, after=after, before=before)
        signature = json.dumps(filters, sort_keys=True)
        if st.session_state.get("_history_filters") != signature:
            st.session_state["history_page"] = 1
            st.session_state["_history_filters"] = signature
        total, records = store.history(**filters)
        offset = page_offset(total, "history_page")
        if offset:
            _, records = store.history(**filters, offset=offset)
        if not records:
            st.info("검색 조건에 맞는 실행이 없습니다.")
        else:
            display = run_rows(records)
            st.dataframe(display, hide_index=True, use_container_width=True)
            st.download_button("현재 페이지 CSV 저장", csv_bytes(display), "snapshot-runs.csv", "text/csv")
            by_id = {r["id"]: r for r in records}
            rid = st.selectbox(
                "상세를 볼 실행",
                list(by_id),
                format_func=lambda x: (
                    f"{stamp(by_id[x]['created'])} · {RUN_LABELS[by_id[x]['state']]} · {x[:8]}"
                ),
            )
            r = by_id[rid]
            spec = json.loads(r["spec"])
            st.subheader("실행 당시 작업과 결과")
            st.caption(f"실행 ID: {rid} · 기준일 {spec['date']}")
            detail = []
            for item, j in zip(store.tables(rid), spec["jobs"]):
                src = spec["profiles"][j["source_id"]]
                tgt = spec["profiles"][j["target_id"]]
                detail.append(
                    {
                        "작업": j["name"],
                        "원본": f"{src['host']}:{src['port']}/{src['database']}/{j['source_table']}",
                        "대상": f"{tgt['host']}:{tgt['port']}/{tgt['database']}/{j['target_table']}",
                        "단계": STAGE_LABELS[item["stage"]],
                        "읽기 시작": stamp(item["read_started"]),
                        "읽기 종료": stamp(item["read_ended"]),
                        "추출 행": item["extracted"],
                        "임시 적재 행": item["loaded"],
                        "오류": item["error"] or "",
                    }
                )
            st.dataframe(detail, hide_index=True, use_container_width=True)
            if r["retry_of"]:
                st.caption("재시도한 원래 실행: " + r["retry_of"])
            if st.button("이 실행의 진행·로그·복구 열기", type="primary"):
                st.session_state["view_run"] = rid
                go(5)
            _, events = store.activities(run_id=rid, limit=200)
            with st.expander("요청 및 상태 변경 기록"):
                if events:
                    st.dataframe(activity_rows(events), hide_index=True, use_container_width=True)
                else:
                    st.caption(
                        "활동 기록 기능 추가 전의 실행입니다. 저장된 실행 결과와 설정은 위에서 확인할 수 있습니다."
                    )
    with actions:
        st.caption(
            "작업 생성·수정·삭제, 실행·취소·강제 중단 요청, 상태 변경과 파일 정리를 기록합니다. 사용자 구분은 단일 사용자 앱의 요청/시스템 구분입니다."
        )
        total, events = store.activities()
        offset = page_offset(total, "activities_page")
        if offset:
            _, events = store.activities(offset=offset)
        if events:
            display = activity_rows(events)
            st.dataframe(display, hide_index=True, use_container_width=True)
            st.download_button(
                "현재 페이지 작업 내역 CSV 저장", csv_bytes(display), "snapshot-activity.csv", "text/csv"
            )
        else:
            st.info("아직 기록된 작업 내역이 없습니다. 기능 추가 이후의 활동부터 자동 기록합니다.")
