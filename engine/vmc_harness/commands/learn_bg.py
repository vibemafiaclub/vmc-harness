"""`vmc-harness learn-bg --root <프로젝트>` — 내부 하위 명령. `init`이 학습 후보 추출을 분리된 백그라운드로 띄운다(사람·에이전트가 부르지 않는다).

진행·완료는 로컬 파일 `learning.json`과 events(`learning_started`·`learning_done`), 끝나면 notices에 "배운 판단 N개".
"""


def register(sub):
    p = sub.add_parser("learn-bg")  # 내부용 — 도움말 목록에 설명을 싣지 않는다
    p.add_argument("--root", required=True)
    p.set_defaults(func=_run)


def _run(a):
    from vmc_harness.bootstrap import init
    st = init.learning_job(a.root)
    return 0 if st.get("status") == "done" else 1
