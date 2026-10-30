"""`vmc-harness revert <action|task|commit>` — 에이전트가 사람의 "원복해"로 부른다. git revert + finding + 장부 상태."""

import json
import sys

from vmc_harness.commands._ledger_common import need_root


def register(sub):
    p = sub.add_parser("revert", help="하네스 조치(A-…)·작업(T-…)·커밋을 원복(사람 요청 → finding)")
    p.add_argument("target", help="A-… | T-… | 커밋 해시")
    p.add_argument("--reason", default="사람 요청")
    p.set_defaults(func=run)


def run(args):
    root = need_root()
    if root is None:
        return 2
    from vmc_harness.ledger import revert
    try:
        res = revert.revert(root, args.target, args.reason, by_human=True)
    except KeyError as e:
        print("대상 없음: %s" % e, file=sys.stderr)
        return 1
    except revert.RevertDeferred as e:
        print("원복 못 함: %s" % e, file=sys.stderr)
        return 1
    if isinstance(res, dict) and res.get("deferred"):
        print("원복 미룸: %s (다음 판정 때 다시)" % res["deferred"])
        return 0
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0
