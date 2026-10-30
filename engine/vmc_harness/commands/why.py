"""`vmc-harness why <task|file> [--without <id>]` — 그 작업에 영향을 준 것(spec 04)."""

import json
import sys

from vmc_harness.commands._ledger_common import need_root


def register(sub):
    p = sub.add_parser("why", help="그 작업·파일에 영향을 준 규칙·카드·게이트·PM 의견·자동 변경")
    p.add_argument("target", help="T-… 작업 id 또는 파일 경로")
    p.add_argument("--without", default=None, help="그 지침 하나만 끄고 별도 worktree에서 다시 돌려 비교(비용 큼)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=run)


def run(args):
    root = need_root()
    if root is None:
        return 2
    from vmc_harness.ledger import why
    if args.without:
        try:
            res = why.run_without(root, args.target, args.without)
        except (KeyError, ValueError, RuntimeError) as e:
            print("다시 돌리기 못 함: %s" % e, file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print("%s 없이 다시 돌린 결과 (기준 %s)" % (res["without"], res["base"]))
            print("실제 작업 변경:\n%s" % (res["actual_stat"] or "(커밋 없음)"))
            print("다시 돌린 변경:\n%s" % (res["rerun_stat"] or "(변경 없음)"))
            print("전체 diff: %s" % res["diff_file"])
        return 0
    res = why.collect(root, args.target)
    print(json.dumps(res, ensure_ascii=False, indent=2) if args.json else why.render_text(root, res))
    return 0
