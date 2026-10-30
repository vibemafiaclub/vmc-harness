"""`vmc-harness qa` — QA 결과 원본·시나리오·계정·보고서 (spec 05).

  qa run [id]                     시나리오 열기(다음 할 것 자동) — 목표·기대값(출처)·제약·결과 기록 형식 출력
  qa status [--human] [--json]    현재 상태 / 사람 필요·DB 변경 필요 목록만
  qa status set <id> <PASS|FAIL|HOLD> --cause … --needs-human yes|no --needs-db-change yes|no [--evidence …]
  qa resume [--json]              새 세션 "이어서" — 남은 것·다음 할 것
  qa report                       HTML 보고서(보기 전용) 생성
  qa accounts add|list|remove|copy
  qa scenario check [파일…] | save <파일|->
  qa policy show | check --entity E --op OP
  qa agent install|remove         QA 서브에이전트 정의(init이 자동으로 한다)
"""

import json
import sys

from vmc_harness.core import paths
from vmc_harness.core.ledger import MetaGateError


def register(sub):
    p = sub.add_parser("qa", help="QA 결과·시나리오·계정")
    s = p.add_subparsers(dest="qa_cmd")

    r = s.add_parser("run", help="시나리오 열기")
    r.add_argument("scenario_id", nargs="?")
    r.set_defaults(func=cmd_run)

    st = s.add_parser("status", help="상태 보기 / status set으로 갱신")
    st.add_argument("rest", nargs="*", help="set <id> <PASS|FAIL|HOLD>")
    st.add_argument("--human", action="store_true", help="사람 필요·DB 변경 필요 목록만")
    st.add_argument("--json", action="store_true")
    st.add_argument("--cause")
    st.add_argument("--needs-human", choices=("yes", "no"))
    st.add_argument("--needs-db-change", choices=("yes", "no"))
    st.add_argument("--evidence", action="append", default=[])
    st.add_argument("--by", choices=("agent", "human"), default="agent")
    st.add_argument("--reset-budget", action="store_true", help="(사람) 예산 시계를 새로 시작")
    st.set_defaults(func=cmd_status)

    rs = s.add_parser("resume", help="이어서 — 남은 것·다음 할 것")
    rs.add_argument("--json", action="store_true")
    rs.set_defaults(func=cmd_resume)

    rp = s.add_parser("report", help="HTML 보고서 생성(보기 전용)")
    rp.set_defaults(func=cmd_report)

    ac = s.add_parser("accounts", help="테스트 계정(비밀번호는 키체인)")
    acs = ac.add_subparsers(dest="acc_cmd")
    a1 = acs.add_parser("add")
    a1.add_argument("name")
    a1.add_argument("--username", required=True)
    a1.add_argument("--role")
    a1.add_argument("--note")
    a1.set_defaults(func=cmd_acc_add)
    a2 = acs.add_parser("list")
    a2.add_argument("--json", action="store_true")
    a2.set_defaults(func=cmd_acc_list)
    a3 = acs.add_parser("remove")
    a3.add_argument("name")
    a3.set_defaults(func=cmd_acc_remove)
    a4 = acs.add_parser("copy", help="값을 출력하지 않고 클립보드로")
    a4.add_argument("name")
    a4.add_argument("--username", action="store_true", help="아이디를 복사(기본은 비밀번호)")
    a4.set_defaults(func=cmd_acc_copy)
    ac.set_defaults(func=cmd_acc_list, json=False)

    sc = s.add_parser("scenario", help="시나리오 출처 검사·저장")
    scs = sc.add_subparsers(dest="sc_cmd")
    c1 = scs.add_parser("check")
    c1.add_argument("files", nargs="*")
    c1.set_defaults(func=cmd_sc_check)
    c2 = scs.add_parser("save")
    c2.add_argument("file", help="JSON 파일 또는 - (stdin)")
    c2.set_defaults(func=cmd_sc_save)

    po = s.add_parser("policy", help="테스트 데이터 범위")
    pos = po.add_subparsers(dest="po_cmd")
    p1 = pos.add_parser("show")
    p1.set_defaults(func=cmd_po_show)
    p2 = pos.add_parser("check")
    p2.add_argument("--entity", required=True)
    p2.add_argument("--op", required=True, choices=("create", "update", "delete"))
    p2.add_argument("--record")
    p2.set_defaults(func=cmd_po_check)

    ag = s.add_parser("agent", help="QA 서브에이전트 정의 설치·제거")
    ag.add_argument("action", choices=("install", "remove"))
    ag.set_defaults(func=cmd_agent)

    p.set_defaults(func=cmd_resume, json=False)


def _root():
    root = paths.find_project_root()
    if not root:
        print("하네스가 설치되지 않은 프로젝트", file=sys.stderr)
    return root


def _yn(v):
    return None if v is None else v == "yes"


def cmd_run(args):
    root = _root()
    if not root:
        return 2
    from vmc_harness.qa import runner
    try:
        sc, rec, pol = runner.start(root, args.scenario_id)
    except runner.RunRefused as e:
        print(str(e), file=sys.stderr)
        return 1
    print(runner.brief_text(root, sc, pol))
    return 0


def cmd_status(args):
    root = _root()
    if not root:
        return 2
    from vmc_harness.qa import results, runner
    rest = args.rest or []
    if rest:
        if rest[0] != "set" or len(rest) != 3:
            print("형식: qa status set <id> <PASS|FAIL|HOLD>", file=sys.stderr)
            return 2
        try:
            rec = runner.finish(root, rest[1], rest[2], cause=args.cause, needs_human=_yn(args.needs_human),
                                needs_db_change=_yn(args.needs_db_change), evidence=args.evidence, by=args.by,
                                reset_budget=args.reset_budget)
        except MetaGateError as e:
            print(str(e), file=sys.stderr)
            return 1
        print("%s · %s" % (rest[1], rec["status"]))
        if rec["status"] == "HOLD" and (rec.get("budget") or {}).get("exceeded"):
            print("HOLD — 사람 확인 목록으로 넘어감")
        return 0
    if args.human:
        items = results.human_list(root)
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
            return 0
        if not items:
            print("사람이 볼 것 없음")
            return 0
        print("사람이 볼 것 %d건" % len(items))
        for h in items:
            print("- %s (%s) %s" % (h["id"], ", ".join(h["reasons"]), h.get("cause") or ""))
        return 0
    data = results.load(root)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    s = results.summary(root)
    print("전체 %d · PASS %d · FAIL %d · HOLD %d · 미실행 %d" % (s["total"], s["PASS"], s["FAIL"], s["HOLD"], s["PENDING"]))
    for sid, r in sorted(data["scenarios"].items()):
        print("- %s %s %s" % (sid, r.get("status"), r.get("cause") or ""))
    return 0


def cmd_resume(args):
    root = _root()
    if not root:
        return 2
    from vmc_harness.qa import results
    r = results.resume(root)
    if getattr(args, "json", False):
        print(json.dumps(r, ensure_ascii=False, indent=2))
        return 0
    s = r["summary"]
    print("QA 이어서 — 전체 %d · PASS %d · FAIL %d · HOLD %d · 미실행 %d" %
          (s["total"], s["PASS"], s["FAIL"], s["HOLD"], s["PENDING"]))
    if r["next"]:
        print("다음: vmc-harness qa run %s (%s)" % (r["next"]["id"], r["next"]["status"]))
    else:
        print("다음: 없음 — 사람 확인 대기만 남음" if r["human"] else "다음: 없음")
    if r["remaining"]:
        print("남은 것: %s" % ", ".join("%s(%s)" % (x["id"], x["status"]) for x in r["remaining"]))
    if r["human"]:
        print("사람 확인 대기: %s" % ", ".join("%s(%s)" % (h["id"], "·".join(h["reasons"])) for h in r["human"]))
    for bad in r["invalid"]:
        print("형식 오류 시나리오: %s — %s" % (bad["id"], bad["reason"]))
    return 0


def cmd_report(args):
    root = _root()
    if not root:
        return 2
    from vmc_harness.qa import report_html
    print(report_html.build(root))
    return 0


def cmd_acc_add(args):
    root = _root()
    if not root:
        return 2
    if not sys.stdin.isatty():
        print("비밀번호는 터미널에서 직접 입력해야 함(대화·파이프 입력 불가)", file=sys.stderr)
        return 2
    from vmc_harness.core import secrets
    from vmc_harness.qa import accounts
    pw = secrets.read_secret_from_terminal("비밀번호(%s): " % args.name)
    try:
        accounts.add(root, args.name, args.username, pw, role=args.role, note=args.note)
    except NotImplementedError:
        print("키체인 저장 기능 없음(설치본 확인 필요)", file=sys.stderr)
        return 1
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 1
    print("저장함: %s" % args.name)
    return 0


def cmd_acc_list(args):
    root = _root()
    if not root:
        return 2
    from vmc_harness.qa import accounts
    items = accounts.listing(root)
    if getattr(args, "json", False):
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return 0
    if not items:
        print("계정 없음 — 사람이 터미널에서 `vmc-harness qa accounts add <이름> --username <아이디>`")
    for a in items:
        print("- %s  %s  %s" % (a["name"], a["username"], a.get("role") or ""))
    return 0


def cmd_acc_remove(args):
    root = _root()
    if not root:
        return 2
    from vmc_harness.qa import accounts
    print("삭제함" if accounts.remove(root, args.name) else "없음")
    return 0


def cmd_acc_copy(args):
    root = _root()
    if not root:
        return 2
    from vmc_harness.qa import accounts
    try:
        ok, reason = accounts.copy_to_clipboard(root, args.name, "username" if args.username else "password")
    except NotImplementedError:
        ok, reason = False, "키체인 읽기 기능 없음"
    if ok:
        print("클립보드에 복사함(값은 출력하지 않음)")
        return 0
    print(reason, file=sys.stderr)
    return 1


def cmd_sc_check(args):
    root = _root()
    if not root:
        return 2
    from vmc_harness.qa import scenarios
    bad = 0
    if args.files:
        from vmc_harness.core.jsonio import read_json
        rows = []
        for f in args.files:
            try:
                sc = read_json(f)
            except ValueError:
                rows.append((f, False, "JSON 형식 오류"))
                continue
            ok, reason = scenarios.is_valid(sc)
            rows.append((f, ok, reason))
    else:
        rows = [(sc.get("id"), ok, reason) for sc, ok, reason in scenarios.load_all(root)]
    for name, ok, reason in rows:
        print("%s %s%s" % ("OK " if ok else "거부", name, "" if ok else " — %s" % reason))
        bad += 0 if ok else 1
    return 1 if bad else 0


def cmd_sc_save(args):
    root = _root()
    if not root:
        return 2
    from vmc_harness.qa import scenarios
    text = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
    try:
        sc = json.loads(text)
        rec = scenarios.save(root, sc)
    except ValueError as e:
        print(str(e) if isinstance(e, MetaGateError) else "JSON 형식 오류", file=sys.stderr)
        return 1
    print("저장함: .harness/qa/scenarios/%s.json" % rec["id"])
    return 0


def cmd_po_show(args):
    root = _root()
    if not root:
        return 2
    from vmc_harness.qa import policy
    print(json.dumps(policy.ensure(root), ensure_ascii=False, indent=2))
    return 0


def cmd_po_check(args):
    root = _root()
    if not root:
        return 2
    from vmc_harness.qa import policy
    ok, reason = policy.check_write(root, {"kind": "data", "entity": args.entity, "op": args.op, "record": args.record})
    print("허용" if ok else "거부 — %s" % reason)
    return 0 if ok else 1


def cmd_agent(args):
    root = _root()
    if not root:
        return 2
    from vmc_harness.qa import install
    if args.action == "install":
        print(json.dumps(install.register(root), ensure_ascii=False))
    else:
        print(json.dumps(install.unregister(root), ensure_ascii=False))
    return 0
