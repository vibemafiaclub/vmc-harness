"""`vmc-harness notice …` — 패널·대화창 버튼(spec 08). 버튼 누름 = 사람의 명시적 행동(finding은 엔진이).

  notice mute <id>        [알림 끄기] → finding(button=mute) + 그 종류 감소(core.notices.mute)
  notice unmute <kind>    끈 알림 종류 다시 켜기(되돌리기)
  notice shown <id…>      보인 알림(다시 알리지 않음)
  notice on <id>          [켜기] 꺼진 사람 요청 게이트 다시 켜기 / notice keep <id> [유지](기본값, 기록만)
  notice delete <action>  [삭제] 배운 판단 지우기 → finding(button=delete) + 원복
  notice add|exclude <번호>  [추가]/[제외] 진단 제안(bootstrap.diagnose.accept/exclude — [제외] = finding)
  notice revert <id>      [원복] 정정과 부딪힌 지침 원복 후보 → `finding --blame <지침 id>`와 같은 경로(why.blame)
  notice dismiss <id>     [아님] 원복 후보 아님 — 기록만(finding 아님, 지침 존속 근거도 아님)
"""

import json


def register(sub):
    p = sub.add_parser("notice", help="알림 버튼: mute·shown·on·keep·delete·add·exclude·revert·dismiss")
    p.add_argument("action", choices=("mute", "unmute", "shown", "on", "keep", "delete", "add", "exclude", "revert", "dismiss"))
    p.add_argument("ids", nargs="+")
    p.set_defaults(func=_run)


def _notice(root, nid):
    from vmc_harness.core.jsonio import read_jsonl
    from vmc_harness.core.project import local_dir
    for n in read_jsonl(local_dir(root) / "notices.jsonl"):
        if n.get("id") == nid:
            return n
    return None


def _run(a):
    from vmc_harness.core import ledger, notices, paths
    from vmc_harness.render import brief
    root = paths.find_project_root()
    if not root:
        print("하네스가 설치되지 않은 프로젝트")
        return 2
    out = {"action": a.action, "ids": a.ids}
    if a.action == "mute":
        for nid in a.ids:
            try:
                out.setdefault("findings", []).append(notices.mute(root, nid)["finding_id"])
            except KeyError:
                print("알림 없음: %s" % nid)
                return 2
        brief.mark_seen(root, a.ids)
    elif a.action == "unmute":
        notices.unmute(root, a.ids)
    elif a.action == "shown":
        brief.mark_seen(root, a.ids)
    elif a.action == "keep":
        for nid in a.ids:
            gid = ((_notice(root, nid) or {}).get("refs") or {}).get("gate_id")
            if gid:
                try:
                    from vmc_harness.gates import generated
                    generated.keep(root, gid)
                except (ImportError, AttributeError, KeyError):
                    pass
        brief.mark_seen(root, a.ids)
    elif a.action == "on":
        for nid in a.ids:
            n = _notice(root, nid) or {}
            gid = (n.get("refs") or {}).get("gate_id")
            if not gid:
                print("게이트가 없는 알림: %s" % nid)
                return 2
            ok = False
            try:
                from vmc_harness.gates import generated
                ok = bool(generated.enable(root, gid))
            except (ImportError, AttributeError):
                from vmc_harness.ledger import revert
                ok = revert.set_gate_status(root, gid, "active")
            if not ok:
                print("게이트를 켜지 못함: %s" % gid)
                return 2
        brief.mark_seen(root, a.ids)
    elif a.action == "delete":
        from vmc_harness.ledger import revert
        for aid in a.ids:
            ledger.create_finding(root, "button", {"action_id": aid}, button="delete")
            revert.revert_action(root, aid, "패널 [삭제]", by_human=True, make_finding=False)
    elif a.action in ("revert", "dismiss"):
        from vmc_harness.core import events
        from vmc_harness.ledger import why
        for nid in a.ids:
            gid = ((_notice(root, nid) or {}).get("refs") or {}).get("guide_id")
            if not gid:
                print("원복 후보 알림이 아님: %s" % nid)
                return 2
            if a.action == "revert":
                try:
                    out.setdefault("results", []).append(why.blame(root, gid))
                except (KeyError, ValueError) as e:
                    print("원복 못 함: %s" % e)
                    return 2
            else:
                events.log(root, "guide_conflict_dismissed", notice_id=nid, guide_id=gid)
        brief.mark_seen(root, a.ids)
    elif a.action in ("add", "exclude"):
        try:
            from vmc_harness.bootstrap import diagnose
            fn = diagnose.accept if a.action == "add" else diagnose.exclude
        except (ImportError, AttributeError):
            print("진단 제안 기능 없음")
            return 2
        try:
            out["results"] = fn(root, [int(x) for x in a.ids])
        except ValueError as e:
            print(str(e))
            return 2
    print(json.dumps(out, ensure_ascii=False))
    return 0
