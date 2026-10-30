"""`vmc-harness approval <id> approve|reject|detail|shown|card` — 승인 카드(spec 08).

대화창·패널 어느 쪽에서 눌러도 같은 기록 하나(core.ledger.answer_approval)가 처리된다. 거절 = finding(코어).
"""

import json


def register(sub):
    p = sub.add_parser("approval", help="승인 카드: 승인·거절·자세히")
    p.add_argument("approval_id")
    p.add_argument("action", choices=("approve", "reject", "detail", "shown", "card"))
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_run)


def _run(a):
    from vmc_harness.core import paths
    from vmc_harness.render import approval_card
    root = paths.find_project_root()
    if not root:
        print("하네스가 설치되지 않은 프로젝트")
        return 2
    try:
        if a.action in ("approve", "reject"):
            rec = approval_card.answer(root, a.approval_id, a.action)
            out = {"approval_id": a.approval_id, "answer": rec.get("answer"), "answered_at": rec.get("answered_at"),
                   "answer_sec": approval_card.answer_sec(rec)}
            print(json.dumps(out, ensure_ascii=False) if a.json else ("승인" if rec.get("answer") == "approve" else "거절"))
        elif a.action == "detail":
            approval_card.mark_shown(root, a.approval_id)
            if a.json:
                print(json.dumps(approval_card.detail(root, a.approval_id), ensure_ascii=False))
            else:
                print(approval_card.detail_text(root, a.approval_id))
        elif a.action == "shown":
            approval_card.mark_shown(root, a.approval_id)
            print(json.dumps({"approval_id": a.approval_id, "shown": True}) if a.json else "ok")
        else:
            c = approval_card.card(root, a.approval_id)
            approval_card.mark_shown(root, a.approval_id)
            print(json.dumps(c, ensure_ascii=False) if a.json else "\n".join(c["lines"] + ["승인 · 거절 · 자세히"]))
    except KeyError:
        print("승인 기록 없음: %s" % a.approval_id)
        return 2
    except approval_card.CardError as e:
        print(str(e))
        return 2
    return 0
