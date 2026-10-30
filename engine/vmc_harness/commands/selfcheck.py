"""`vmc-harness selfcheck` — 세션 종료 자가 점검(훅이 분리 실행). 사람 개입 수·학습 후보·명시 요청 조치."""

import json

from vmc_harness.commands._ledger_common import need_root


def register(sub):
    p = sub.add_parser("selfcheck", help="세션 기록 자가 점검(사람 개입·학습 후보·명시 요청)")
    p.add_argument("--session", help="세션 기록 파일 경로(없으면 지난 세션 중 미점검분)")
    p.add_argument("--agent", default="claude", choices=("claude", "codex"))
    p.add_argument("--session-id", default=None)
    p.add_argument("--no-llm", action="store_true", help="학습 후보 추출 없이 숫자만")
    p.add_argument("--stale-docs", action="store_true", help="낡은 문서 검출만")
    p.set_defaults(func=run)


def run(args):
    root = need_root()
    if root is None:
        return 2
    if args.stale_docs:
        from vmc_harness.ledger import stale_docs
        print(json.dumps(stale_docs.scan(root), ensure_ascii=False, indent=2))
        return 0
    from vmc_harness.ledger import selfcheck
    if args.session:
        res = selfcheck.run_session(root, args.session, args.agent, args.session_id, use_llm=not args.no_llm)
    else:
        res = selfcheck.run_pending(root)
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0
