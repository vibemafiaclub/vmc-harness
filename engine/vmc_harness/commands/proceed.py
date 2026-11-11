"""`vmc-harness proceed` — 게이트가 잘못 막았을 때 사람이 "그냥 진행"(spec 00 기준 3).

가장 최근 차단 1건을 헛막기 finding(false_block)으로 남기고 그 호출을 한 번 통과시킨다.
사람이 터미널에서 직접 친 경우만 인정한다(에이전트 셸 도구에서 부르면 거부 — 01 P2 사람의 명시적 행동).
대화창에서는 차단 직후 "그냥 진행"이라고 말하면 같은 처리가 된다(UserPromptSubmit 훅).
`--dispute "<근거>"`는 에이전트도 쓸 수 있다 — 반박을 참고 지표로만 남기고 통과·finding은 없다(01 P2).
"""

import sys

from vmc_harness.core import paths


def register(sub):
    p = sub.add_parser("proceed", help="게이트가 잘못 막았을 때 그냥 진행(헛막기 기록)")
    p.add_argument("--session", help="세션 id(없으면 환경변수 → 가장 최근 차단)")
    p.add_argument("--dispute", metavar="근거", help="잘못 막았다는 근거 한 줄 — 참고 지표로만 남는다(통과되지 않음, 에이전트도 사용 가능)")
    p.set_defaults(func=run)


def run(args):
    root = paths.find_project_root()
    if root is None:
        print("하네스가 설치된 프로젝트가 아님(.harness/config.json 없음)", file=sys.stderr)
        return 2
    from vmc_harness.gates import proceed
    if args.dispute is not None:
        from vmc_harness.task import state as tstate
        try:
            b = proceed.dispute(root, args.session or tstate.env_session_id(), args.dispute)
        except proceed.ProceedError as e:
            print("반박 못 남김: %s" % e, file=sys.stderr)
            return 1
        print("반박을 참고 지표로 남겼어요(%s 차단 %s). 통과되지 않아요 — 판정은 사람의 '그냥 진행'으로만 바뀌어요." % (b["gate"], b.get("at")))
        return 0
    try:
        f = proceed.from_cli(root, args.session)
    except proceed.ProceedError as e:
        print("진행 못 함: %s" % e, file=sys.stderr)
        return 1
    print("그냥 진행으로 기록했어요(%s). 같은 작업을 다시 시키면 이번 한 번은 통과해요." % f["finding_id"])
    return 0
