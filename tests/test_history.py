from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from snapshot.history_ui import csv_bytes
from snapshot.store import Store
from snapshot.workflow import HISTORY_PAGES, STEPS


def seed(store):
    base = dict(host="localhost", port=3306, user="user", tls=False)
    store.save("connection_profile", dict(base, id="s", role="source", name="원본", database="source"))
    store.save("connection_profile", dict(base, id="t", role="target", name="대상", database="target"))
    job = dict(
        id="j",
        name="주문 복사",
        source_id="s",
        target_id="t",
        source_table="orders",
        target_table="orders_copy",
        read_mode="pk",
        batch_rows=1000,
        wait_ms=300,
        connect_timeout=10,
        read_timeout=60,
        write_timeout=60,
        retries=2,
    )
    store.save("backup_job", job)
    spec = dict(
        jobs=[job],
        profiles={p["id"]: p for p in store.profiles()},
        date="2026-09-06",
        limit=100,
        work_dir=str(store.root / "files"),
    )
    return job, spec


def complete(store, spec, state="SUCCESS", retry_of=None):
    rid = store.queue(spec, retry_of)
    store.update_run(rid, state=state, started=100, ended=110)
    store.update_table(rid, 0, stage="READY" if state == "SUCCESS" else "FAILED", extracted=100, loaded=100)
    return rid


def test_events_transactional_persistent_and_no_heartbeat_noise(tmp_path):
    store = Store(tmp_path)
    job, spec = seed(store)
    store.save("backup_job", dict(job, name="수정된 주문"))
    rid = store.queue(spec)
    count = store.activities()[0]
    with pytest.raises(ValueError):
        store.save("backup_job", dict(job, name="차단된 수정"))
    assert store.activities()[0] == count
    store.update_run(rid, heartbeat=1234)
    assert store.activities()[0] == count
    store.cancel(rid)
    store.cancel(rid)
    store.update_run(rid, state="CANCELLED", ended=110)
    store.delete("backup_job", "j")
    reopened = Store(tmp_path)
    _, records = reopened.activities(limit=200)
    actions = [r["action"] for r in records]
    assert actions.count("CANCEL_REQUESTED") == 1
    assert {"JOB_CREATED", "JOB_UPDATED", "JOB_DELETED", "RUN_REQUESTED", "STATE_CHANGED"} <= set(actions)
    assert reopened.history()[0] == 1
    # Names are from the immutable run spec, not the edited/deleted definition.
    item = reopened.job_overview()[0]
    assert item["deleted"] and item["job"]["name"] == "주문 복사" and item["count"] == 1


def test_all_history_pages_filters_and_literal_search(tmp_path):
    store = Store(tmp_path)
    _, spec = seed(store)
    ids = [
        complete(
            store, dict(spec, limit=100 if i % 2 == 0 else None), state="SUCCESS" if i % 2 == 0 else "FAILED"
        )
        for i in range(105)
    ]
    with store.db() as con:
        con.executemany("UPDATE run SET created=? WHERE id=?", [(i, rid) for i, rid in enumerate(ids)])
    assert len(store.runs()) == 100
    total, page1 = store.history()
    assert total == 105 and len(page1) == 50 and page1[0]["id"] == ids[-1]
    assert [r["id"] for r in store.history(offset=100)[1]] == ids[4::-1]
    assert store.history(state="FAILED", mode="full", job_id="j")[0] == 52
    assert store.history(after=10, before=20)[0] == 10
    assert store.history(keyword="orders_copy")[0] == 105
    assert store.history(keyword="주문")[0] == 105
    assert store.history(keyword="%' OR 1=1 --")[0] == 0
    assert store.history(keyword="%")[0] == 0
    assert store.history()[1][0]["extracted"] == 100
    assert store.job_overview()[0]["count"] == 105


def test_legacy_database_keeps_runs_without_fabricating_actions(tmp_path):
    store = Store(tmp_path)
    _, spec = seed(store)
    rid = complete(store, spec)
    with store.db() as con:
        con.execute("DROP TABLE activity_log")
    reopened = Store(tmp_path)
    assert reopened.history()[0] == 1
    assert reopened.run(rid)["spec"]["jobs"][0]["name"] == "주문 복사"
    assert reopened.activities()[0] == 0


def test_retry_links_and_csv_safety(tmp_path):
    store = Store(tmp_path)
    _, spec = seed(store)
    old = complete(store, spec, state="FAILED")
    new = complete(store, spec, retry_of=old)
    assert store.run(new)["retry_of"] == old
    _, events = store.activities(run_id=new)
    assert next(r for r in events if r["action"] == "RETRY_REQUESTED")["detail"] == old
    csv = csv_bytes([{"작업": '=HYPERLINK("bad")', "행 수": 100}]).decode("utf-8-sig")
    assert "'=HYPERLINK" in csv


def test_history_menu_and_deleted_job_detail_beyond_recent_100(tmp_path, monkeypatch):
    store = Store(tmp_path)
    job, spec = seed(store)
    old = complete(store, spec)
    for _ in range(101):
        complete(store, dict(spec, jobs=[dict(job, name="최근 주문")]))
    store.delete("backup_job", "j")
    monkeypatch.setenv("SNAPSHOT_HOME", str(tmp_path))
    # Browsing must work without contacting source or target.
    import snapshot.db

    monkeypatch.setattr(
        snapshot.db, "connect", lambda *a, **kw: pytest.fail("history must not connect to MariaDB")
    )
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=20)
    app.sidebar.radio[0].set_value(HISTORY_PAGES[0]).run()
    assert not app.exception
    next(c for c in app.checkbox if c.label == "삭제된 작업의 실행 기록도 포함").check().run()
    next(b for b in app.button if b.label == "이 작업의 실행 이력").click().run()
    assert app.sidebar.radio[0].value == HISTORY_PAGES[1]
    assert not app.exception
    next(e for e in app.text_input if e.label.startswith("실행 검색")).set_value(old).run()
    assert not app.exception
    next(b for b in app.button if b.label == "이 실행의 진행·로그·복구 열기").click().run()
    assert not app.exception
    assert app.sidebar.radio[0].value == STEPS[4]
    assert app.selectbox[0].value == old
    assert any("주문 복사" in c.value for c in app.caption)


def test_job_menu_opens_existing_settings(tmp_path, monkeypatch):
    store = Store(tmp_path)
    seed(store)
    monkeypatch.setenv("SNAPSHOT_HOME", str(tmp_path))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py")).run(timeout=20)
    app.sidebar.radio[0].set_value(HISTORY_PAGES[0]).run()
    next(b for b in app.button if b.label == "작업 설정 열기").click().run()
    assert not app.exception
    assert app.sidebar.radio[0].value == STEPS[1]
    assert app.selectbox[0].value == "j"
