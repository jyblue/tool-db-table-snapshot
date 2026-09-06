"""Presentation decisions for the guided workflow; no database or UI I/O."""

import json

STEPS = ["1. 연결 설정", "2. 복사 작업", "3. 소량 테스트", "4. 전체 실행", "5. 결과·복구"]
RUN_LABELS = {
    "QUEUED": "시작 준비 중",
    "RUNNING": "복사 중",
    "PUBLISHING": "최종 저장 중",
    "SUCCESS": "완료",
    "CANCEL_REQUESTED": "취소 처리 중",
    "CANCELLED": "취소됨",
    "FAILED": "실패",
    "INTERRUPTED": "중단됨",
}
STAGE_LABELS = {
    "WAITING": "대기",
    "EXTRACTING": "원본 읽기",
    "SLEEPING": "부하 조절 대기",
    "LOADING": "대상 임시 적재",
    "VALIDATING": "검증",
    "READY": "검증 완료",
    "PUBLISHED": "최종 저장 완료",
    "FAILED": "실패",
    "CANCELLED": "취소됨",
}


def initial_step(profiles, jobs, runs):
    if any(
        r["state"] in ("QUEUED", "RUNNING", "PUBLISHING", "CANCEL_REQUESTED") or r["publish_pending"]
        for r in runs
    ):
        return STEPS[4]
    if {p["role"] for p in profiles.values()} != {"source", "target"}:
        return STEPS[0]
    if not jobs:
        return STEPS[1]
    return STEPS[4] if runs else STEPS[2]


def latest_tests(jobs, profiles, runs):
    result = {}
    for run in runs:
        spec = json.loads(run["spec"]) if isinstance(run["spec"], str) else run["spec"]
        if spec["limit"] is None:
            continue
        for saved in spec["jobs"]:
            jid = saved["id"]
            if jid in result or jobs.get(jid) != saved:
                continue
            if all(
                spec["profiles"].get(i) == profiles.get(i) for i in (saved["source_id"], saved["target_id"])
            ):
                result[jid] = run
    return result
