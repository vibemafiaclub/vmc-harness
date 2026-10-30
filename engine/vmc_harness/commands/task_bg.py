"""`vmc-harness task-bg <task_id>` — 내부 하위 명령. 착수 선언·Stop이 백그라운드로 띄운다(사람·에이전트가 부르지 않는다).

기본: PM + 카드 검색 묶음(spec 03 §PM). `--overdesign`: 과설계 LLM 판정(요청에 없는 옵션·기능).
`--measure`: 마감된 작업 실측(세션 기록 파싱·git — task.lifecycle). task_id가 `-`면 밀린 것 전부.
결과는 로컬 파일 `tasks/<id>.bg.json` / `.od.json`.
"""

import argparse


def register(sub):
    p = sub.add_parser("task-bg")  # 내부용 — 도움말 목록에 설명을 싣지 않는다
    p.add_argument("task_id")
    p.add_argument("--root", required=True)
    p.add_argument("--overdesign", action="store_true")
    p.add_argument("--measure", action="store_true")
    p.add_argument("--transcript")
    p.add_argument("--agent")
    p.set_defaults(func=_run)


def _run(a):
    if a.measure:
        from vmc_harness.task import lifecycle, state as st
        if a.task_id in ("-", ""):
            lifecycle.measure_pending(a.root)
        else:
            t = st.by_id(a.root, a.task_id)
            if t and t.get("measure_pending"):
                lifecycle.measure_one(a.root, t)
        return 0
    if a.overdesign:
        from vmc_harness.task import overdesign
        overdesign.run_llm(a.root, a.task_id, a.transcript, a.agent)
    else:
        from vmc_harness.task import pm
        pm.run_bundle(a.root, a.task_id)
    return 0
