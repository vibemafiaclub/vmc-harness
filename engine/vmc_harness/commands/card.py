"""`vmc-harness card` — 브레인 카드 보기·추가(사람 명시 요청)·삭제(= finding 즉시)·충돌 기록(spec 04)."""

import json
import sys

from vmc_harness.core import paths


def register(sub):
    p = sub.add_parser("card", help="브레인 카드(list·show·add·delete·conflict)")
    s = p.add_subparsers(dest="card_cmd")
    l = s.add_parser("list", help="활성 카드 목록")
    l.add_argument("--all", action="store_true")
    sh = s.add_parser("show")
    sh.add_argument("id")
    a = s.add_parser("add", help="사람이 명시적으로 요청한 판단을 카드로(원문 그대로)")
    a.add_argument("--title", required=True)
    a.add_argument("--judgment", required=True)
    a.add_argument("--quote", required=True, help="사람이 한 말 그대로")
    a.add_argument("--situation", default="")
    a.add_argument("--signal", action="append", default=[])
    a.add_argument("--not-when", default="")
    a.add_argument("--why-not-higher", required=True, help="구조·게이트·규칙이 아니라 카드인 이유 한 줄(200자 이하)")
    a.add_argument("--session-id", default=None)
    d = s.add_parser("delete", help="카드 삭제(= finding, 즉시)")
    d.add_argument("id")
    c = s.add_parser("conflict", help="카드가 지금 지시와 달라 지시를 따랐음을 기록")
    c.add_argument("id")
    c.add_argument("--note", default=None)
    p.set_defaults(func=run)


def delete_card(root, card_id, button="delete"):
    from vmc_harness.brain import store
    from vmc_harness.core import ledger
    from vmc_harness.ledger import revert
    if store.get_card(card_id) is None:
        raise KeyError(card_id)
    f = ledger.create_finding(root, "button", {"card": card_id}, button=button) if root else None
    acts = []
    if root:
        for a in ledger.list_actions(root):
            if a and (a.get("target") or {}).get("card") == card_id and a.get("status") in ("trial", "extended", "kept"):
                revert.revert_action(root, a["action_id"], "사람이 카드 [%s]" % button, by_human=True, make_finding=False)
                acts.append(a["action_id"])
    if button == "delete":
        store.set_status(card_id, "deleted", reason="사람이 삭제")
    else:
        store.set_status(card_id, "disabled", reason="사람이 [%s]" % button)
    return {"card": card_id, "finding": f and f["finding_id"], "actions": acts}


def run(args):
    from vmc_harness.brain import store
    root = paths.find_project_root()
    cmd = args.card_cmd
    try:
        if cmd in (None, "list"):
            for c in store.list_cards(include_inactive=getattr(args, "all", False)):
                print("%s  %s%s" % (c["id"], c.get("title"), "" if c.get("status") == "active" else "  (%s)" % c.get("status")))
            return 0
        if cmd == "show":
            c = store.get_card(args.id)
            if not c:
                raise KeyError(args.id)
            print(json.dumps(store.view(c, full=True), ensure_ascii=False, indent=2))
            return 0
        if cmd == "conflict":
            from vmc_harness.brain import conflict
            print(json.dumps(conflict.record(args.id, args.note, root), ensure_ascii=False))
            return 0
        if cmd == "delete":
            print(json.dumps(delete_card(root, args.id), ensure_ascii=False))
            return 0
        if cmd == "add":
            if root is None:
                print("하네스가 설치된 프로젝트가 아님", file=sys.stderr)
                return 2
            from vmc_harness.core.project import project_id
            from vmc_harness.ledger.selfcheck import make_card_action
            from vmc_harness.core.clock import now_iso
            fields = {"title": args.title, "situation": args.situation, "judgment": args.judgment,
                      "signals": args.signal, "not_when": args.not_when, "quote": args.quote,
                      "source": {"project": project_id(root), "session": args.session_id, "date": now_iso()[:10]}}
            cid, a = make_card_action(root, fields, "explicit_request",
                                      {"type": "explicit_request", "quote": args.quote, "ref": args.session_id},
                                      args.why_not_higher, "이 판단 카드로 같은 지시를 다시 하지 않는다", True)
            print(json.dumps({"card": cid, "action_id": a["action_id"]}, ensure_ascii=False))
            return 0
    except KeyError as e:
        print("카드 없음: %s" % e, file=sys.stderr)
        return 1
    except Exception as e:
        from vmc_harness.core.ledger import MetaGateError
        if isinstance(e, MetaGateError):
            print("거부: %s" % e, file=sys.stderr)
            return 1
        raise
    return 0
