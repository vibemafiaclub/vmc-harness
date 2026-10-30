"""`vmc-harness finding` — 사람의 명시적 행동 기록(01 P2). 원인 지목(--blame)은 즉시 원복까지."""

import json
import sys

from vmc_harness.commands._ledger_common import need_root


def register(sub):
    p = sub.add_parser("finding", help="사람의 명시적 행동 기록(원인 지목·패널 버튼)")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--blame", metavar="ID", help="why 후보 중 사람이 원인으로 지목한 것(A-…·K-…·R-…·게이트·커밋·T-…)")
    g.add_argument("--button", choices=("mute", "delete", "exclude"), help="패널 버튼")
    p.add_argument("--target", help="--button 대상(알림 id·카드 id·조치 id·게이트 id)")
    p.set_defaults(func=run)


def run(args):
    root = need_root()
    if root is None:
        return 2
    try:
        if args.blame:
            from vmc_harness.ledger import why
            res = why.blame(root, args.blame)
            _close_conflict_notices(root, args.blame)
        else:
            res = button(root, args.button, args.target)
    except (KeyError, ValueError) as e:
        print("기록 못 함: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
    return 0


def button(root, kind, target):
    if not target:
        raise ValueError("--target 없음")
    from vmc_harness.core import ledger, notices
    if kind == "mute":
        return notices.mute(root, target)
    if target.startswith("K-"):
        from vmc_harness.commands.card import delete_card
        return delete_card(root, target, button=kind)
    key = "action_id" if target.startswith("A-") else "gate_id"
    f = ledger.create_finding(root, "button", {key: target}, button=kind)
    if key == "action_id":
        from vmc_harness.ledger import revert
        revert.revert_action(root, target, "사람이 [%s]" % kind, by_human=True, make_finding=False)
    return f


def _close_conflict_notices(root, guide_id):
    """명령으로 원복했으면 같은 지침의 원복 후보 알림(selfcheck)을 보인 것으로 — 패널에 [원복]이 다시 뜨지 않게."""
    from vmc_harness.core.jsonio import read_jsonl
    from vmc_harness.core.project import local_dir
    from vmc_harness.render import brief
    ids = [n["id"] for n in read_jsonl(local_dir(root) / "notices.jsonl")
           if n.get("kind") == "guide_conflict" and (n.get("refs") or {}).get("guide_id") == guide_id]
    brief.mark_seen(root, ids)
