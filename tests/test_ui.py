import json
from pathlib import Path

from streamlit.testing.v1 import AppTest

from snapshot.store import Store
from snapshot.workflow import NAVIGATION, STEPS, initial_step, latest_tests

APP = Path(__file__).resolve().parents[1] / "app.py"


def button(app, label):
    return next(b for b in app.button if b.label == label)


def setup_profiles(root):
    store = Store(root)
    base = dict(host="localhost", port=3306, user="user", tls=False)
    store.save("connection_profile", dict(base, id="s", role="source", name="원본", database="source"))
    store.save("connection_profile", dict(base, id="t", role="target", name="대상", database="target"))
    return store


def test_first_visit_has_ordered_steps_and_blocked_steps_explain_next_action(tmp_path, monkeypatch):
    monkeypatch.setenv("SNAPSHOT_HOME", str(tmp_path / "state"))
    app = AppTest.from_file(str(APP)).run(timeout=20)
    assert not app.exception
    assert app.sidebar.radio[0].options == NAVIGATION
    assert app.sidebar.radio[0].value == STEPS[0]
    app.sidebar.radio[0].set_value(STEPS[3]).run()
    assert not app.exception
    button(app, "1단계 · 연결 설정으로").click().run()
    assert app.sidebar.radio[0].value == STEPS[0]
    assert not app.exception


def test_save_source_then_target_then_job_then_test(tmp_path, monkeypatch):
    monkeypatch.setenv("SNAPSHOT_HOME", str(tmp_path / "state"))
    app = AppTest.from_file(str(APP)).run(timeout=20)

    def fill_profile(name, database):
        for element in app.text_input:
            if element.label == "연결 이름":
                element.set_value(name)
            elif element.label == "DB명":
                element.set_value(database)
            elif element.label == "사용자":
                element.set_value("readonly")
        button(app, "연결 저장").click().run()
        assert not app.exception

    fill_profile("원본", "source")
    assert any(b.label == "접속 확인" for b in app.button)
    button(app, "다음 · 대상 DB 등록하기").click().run()
    assert not app.exception
    fill_profile("대상", "target")
    button(app, "다음 · 복사 작업 만들기").click().run()
    assert app.sidebar.radio[0].value == STEPS[1]
    next(e for e in app.text_input if e.label == "② 원본 테이블").set_value("orders")
    button(app, "복사 작업 저장").click().run()
    assert not app.exception
    store = Store(tmp_path / "state")
    assert store.jobs()[0]["name"] == "orders"
    assert store.jobs()[0]["target_table"] == "orders"
    button(app, "다음 · 소량 테스트").click().run()
    assert not app.exception
    assert app.sidebar.radio[0].value == STEPS[2]
    assert button(app, "1개 작업 테스트 시작")
    assert not any("전체 실행" in b.label for b in app.button)
    app.sidebar.radio[0].set_value(STEPS[3]).run()
    assert button(app, "1개 작업 전체 실행")
    assert any("성공한 소량 테스트가 없는" in w.value for w in app.warning)


def test_new_connections_start_with_blank_user_values(tmp_path, monkeypatch):
    monkeypatch.setenv("SNAPSHOT_HOME", str(tmp_path / "state"))
    app = AppTest.from_file(str(APP)).run(timeout=20)

    assert next(e for e in app.text_input if e.label == "호스트").value == "localhost"
    assert next(e for e in app.text_input if e.label == "DB명").value == ""
    assert next(e for e in app.text_input if e.label == "사용자").value == ""
    assert next(e for e in app.number_input if e.label == "포트").value == 3306
    assert next(e for e in app.text_input if e.label == "비밀번호").value == ""

    app.radio[0].set_value("target").run()
    assert next(e for e in app.text_input if e.label == "DB명").value == ""
    assert next(e for e in app.number_input if e.label == "포트").value == 3306


def test_start_moves_to_results_and_success_test_reuses_selection(tmp_path, monkeypatch):
    from snapshot import process

    root = tmp_path / "state"
    monkeypatch.setenv("SNAPSHOT_HOME", str(root))
    store = setup_profiles(root)
    job = dict(
        id="j",
        name="주문",
        source_id="s",
        target_id="t",
        source_table="orders",
        target_table="orders",
        read_mode="pk",
        batch_rows=1000,
        wait_ms=300,
        connect_timeout=10,
        read_timeout=60,
        write_timeout=60,
        retries=2,
    )
    store.save("backup_job", job)

    def fake_start(store, spec, secrets):
        rid = store.queue(spec)
        store.update_run(rid, state="SUCCESS")
        store.update_table(rid, 0, stage="READY", extracted=100, loaded=100)
        return rid

    monkeypatch.setattr(process, "start", fake_start)
    app = AppTest.from_file(str(APP)).run(timeout=20)
    assert app.sidebar.radio[0].value == STEPS[2]
    button(app, "1개 작업 테스트 시작").click().run()
    assert not app.exception
    assert app.sidebar.radio[0].value == STEPS[2]
    assert any("대상 DB의 임시 테이블" in message.value for message in app.info)
    button(app, "확인하고 계속").click().run()
    assert not app.exception
    assert app.sidebar.radio[0].value == STEPS[4]
    button(app, "다음 · 같은 작업 전체 실행").click().run()
    assert not app.exception
    assert app.sidebar.radio[0].value == STEPS[3]
    assert app.multiselect[0].value == ["j"]
    assert any("모든 작업" in x.value for x in app.success)


def test_changed_settings_are_not_marked_tested(tmp_path):
    store = setup_profiles(tmp_path)
    job = {"id": "j", "source_id": "s", "target_id": "t", "batch_rows": 1000}
    profiles = {p["id"]: p for p in store.profiles()}
    run = {
        "id": "r",
        "state": "SUCCESS",
        "publish_pending": 0,
        "spec": json.dumps({"limit": 100, "jobs": [job], "profiles": profiles}),
    }
    assert latest_tests({"j": job}, profiles, [run])["j"]["id"] == "r"
    assert latest_tests({"j": dict(job, batch_rows=10)}, profiles, [run]) == {}
    assert initial_step(profiles, {"j": job}, [dict(run, state="RUNNING")]) == STEPS[4]
