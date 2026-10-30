"""`vmc-harness diagnose` — 시니어식 진단·진단 리포트, [추가]·[제외] (spec 07).

    diagnose                 진단 다시 돌리고 리포트(.harness/cache/reports/diagnosis-<date>.html + PDF)
    diagnose accept 1 3      [추가] = 명시 요청 → 게이트·보호 영역·규칙
    diagnose exclude 2       [제외] — 다시 내지 않는다
    diagnose report          진단 없이 리포트만 다시
    diagnose tool-test       (진단 모델용 도구) 테스트를 기준선 방식으로 돌려 새 실패만 요약
    diagnose rebaseline      기준선 다시 재기 — 옛 기준선(Codex 자동 승인 심사 요청이 사람 지시로 섞임) 정정 + latest + 테스트 실패 기준선
    diagnose weekly          정기 재진단 본체(세션 시작 훅이 그 기간(기본 하루) 첫 세션에 백그라운드로 부른다) — 세션 다시 읽기·설치 이후 실측·리포트·알림
"""

import json
import sys

from vmc_harness.core import paths


def register(sub):
    p = sub.add_parser("diagnose", help="시니어식 진단 + 진단 리포트, 제안 [추가]·[제외]")
    p.add_argument("action", nargs="?", default="run",
                   choices=("run", "accept", "exclude", "report", "tool-test", "show", "weekly", "rebaseline"))
    p.add_argument("numbers", nargs="*", help="accept·exclude 번호(예: 1 3 또는 1,3)")
    p.add_argument("--sessions-dir")
    p.add_argument("--analysis-dir")
    p.add_argument("--dir", help="tool-test: 소스 루트 하나만")
    p.add_argument("--no-open", action="store_true")
    p.add_argument("--report-out")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=run)


def _nums(xs):
    out = []
    for x in xs:
        out += [int(t) for t in str(x).replace(",", " ").split() if t.strip().isdigit()]
    return out


def run(args):
    root = paths.find_project_root()
    if root is None:
        print("하네스가 설치된 프로젝트가 아님(.harness/config.json 없음) — 먼저 `vmc-harness init`", file=sys.stderr)
        return 2
    from vmc_harness.bootstrap import diagnose, report, sessions_input
    if args.action == "accept":
        res = diagnose.accept(root, _nums(args.numbers))
        for r in res:
            print(json.dumps(r, ensure_ascii=False) if args.json else _accept_line(r))
        return 0 if all(not r.get("error") for r in res) else 1
    if args.action == "exclude":
        for r in diagnose.exclude(root, _nums(args.numbers)):
            print("%s번 제외%s" % (r["no"], "" if not r.get("error") else " 실패 — " + r["error"]))
        return 0
    if args.action == "tool-test":
        print(json.dumps(diagnose.tool_test(root, args.dir), ensure_ascii=False, indent=2))
        return 0
    if args.action == "weekly":
        from vmc_harness.bootstrap import weekly
        st = weekly.job(root)
        print(json.dumps(st, ensure_ascii=False) if args.json else "정기 재진단: %s — %s" % (st.get("status"), st.get("pdf") or st.get("html") or st.get("error") or ""))
        return 0 if st.get("status") != "error" else 1
    if args.action == "rebaseline":
        from vmc_harness.bootstrap import weekly
        sessions, info = sessions_input.load(root, args.sessions_dir, args.analysis_dir)
        res = weekly.refresh_baselines(root, sessions if info.get("main") else None, info)
        print(json.dumps(res, ensure_ascii=False) if args.json else _rebaseline_line(res))
        return 0
    if args.action == "show":
        print(json.dumps(diagnose.load(root) or {}, ensure_ascii=False, indent=2))
        return 0
    if args.action == "run":
        sessions = None
        prev = diagnose.load(root)
        if args.sessions_dir:
            sessions, _ = sessions_input.load(root, args.sessions_dir, args.analysis_dir)
        elif not diagnose.read_sessions_index(root):
            sessions, _ = sessions_input.load(root, None, args.analysis_dir)
        elif isinstance(prev, dict) and prev.get("status") in ("ok", "no_proposals"):
            # 다시 하는 진단 — 정기 재진단과 같게 세션을 다시 읽고 기준선(정정·latest·테스트 실패)을 갱신한다
            from vmc_harness.bootstrap import weekly
            sessions, info = sessions_input.load(root, None, args.analysis_dir)
            if not info.get("main"):
                sessions = None
            print("기준선 다시 재기: %s" % _rebaseline_line(weekly.refresh_baselines(root, sessions, info)))
        d = diagnose.run(root, sessions=sessions, analysis_dir=args.analysis_dir, progress=print)
        print("제안 %d개" % len(d.get("proposals") or []) if d.get("status") == "ok" else "제안 없음 — %s" % (
            d.get("reason") or "; ".join(d["checks"].get("format_errors") or [])))
    rep = report.build(root, open_browser=not args.no_open, out_dir=args.report_out)
    print("리포트: %s" % rep["html"])
    print("PDF: %s" % (rep.get("pdf") or rep.get("pdf_error")))
    return 0


def _waiting_line(r):
    return " — 꺼 둔 채 넣음, 조건이 맞으면 켬: %s" % ", ".join(r["waiting"]) if r.get("waiting") else ""


def _rebaseline_line(res):
    parts = []
    g = res.get("guardian") or {}
    if g.get("status") == "corrected":
        parts.append("하네스 전 기준선 정정 — 사람 지시 %s건 → %s건(Codex 자동 승인 심사 요청 %s건 제외)" % (
            g.get("human_prompts_before"), g.get("human_prompts_after"), g.get("guardian_requests")))
    elif g.get("status") == "no_sessions":
        parts.append("하네스 전 기준선은 이 PC에 설치 전 세션이 없어 정정하지 못함")
    t = res.get("tests") or {}
    if t:
        parts.append("테스트 실패 기준선 %s → %s건" % (t.get("before"), t.get("after")))
    if res.get("tests_error"):
        parts.append("테스트 기준선 실패 — %s" % res["tests_error"])
    return "; ".join(parts) or "바뀐 것 없음"


def _accept_line(r):
    if r.get("error"):
        return "%s번: 못 넣음 — %s" % (r.get("no"), r["error"])
    if r.get("skipped"):
        return "%s번: %s" % (r.get("no"), r["skipped"])
    k = r.get("kind")
    if k == "protected":
        return "%s번: 보호 영역 추가 %s (조치 %s)" % (r["no"], ", ".join(r.get("added") or []) or "(이미 있음)", r["action_id"])
    if k == "gate":
        if r.get("kernel"):
            return "%s번: 커널 안전 게이트가 이미 막는다 — 요청 기록(조치 %s)" % (r["no"], r["action_id"])
        return "%s번: 게이트 %s (조치 %s)%s%s" % (r["no"], r.get("gate_id"), r.get("action_id"),
                                            (" — " + r["cost_line"]) if r.get("cost_line") else "", _waiting_line(r))
    if k == "rule":
        return "%s번: 규칙 %s (조치 %s)%s" % (r["no"], r.get("rule_id"), r.get("action_id"), _waiting_line(r))
    return "%s번: 구조 개선 요청 기록(조치 %s, 대기)" % (r["no"], r.get("action_id"))
