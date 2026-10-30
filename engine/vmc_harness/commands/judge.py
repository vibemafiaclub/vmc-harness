"""`vmc-harness judge` — 판정 한 바퀴(spec 04). 훅이 세션 시작에 `--background`로 분리 실행한다."""

import json

from vmc_harness.commands._ledger_common import need_root


def register(sub):
    p = sub.add_parser("judge", help="시험 중인 하네스 조치를 노출·명시적 부정으로 판정(존속/원복)")
    p.add_argument("--background", action="store_true", help="세션 시작 분리 실행(판정·자가 점검·낡은 문서 검출)")
    p.add_argument("--exclude-session", default=None, help="지금 진행 중인 세션 id(자가 점검에서 뺀다)")
    p.add_argument("--status", action="store_true", help="판정 없이 시험 진행만 보기")
    p.add_argument("--team", action="store_true", help="팀 합산 집계·참고 지표 보기")
    p.set_defaults(func=run)


def run(args):
    root = need_root()
    if root is None:
        return 2
    if args.background:
        from vmc_harness.ledger.handlers import background_job
        background_job(root, args.exclude_session)
        return 0
    if args.status:
        from vmc_harness.ledger import judge
        print(json.dumps(judge.progress(root), ensure_ascii=False, indent=2))
        return 0
    if args.team:
        from vmc_harness.ledger import metrics
        metrics.rollup(root)
        print(json.dumps(metrics.team(root), ensure_ascii=False, indent=2))
        return 0
    from vmc_harness.ledger import judge
    verdicts = judge.run(root)
    if not verdicts:
        print("판정 바뀐 조치 없음")
    for v in verdicts:
        print("%s %s — %s" % (v["action_id"], v["result"], v["reason"]))
    return 0
