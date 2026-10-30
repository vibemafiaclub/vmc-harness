"""`vmc-harness gate …` — 생성 게이트(L1) 관리 (spec 02 §생성 게이트).

    gate add --quote "<사람 원문>" --session S --area A --paths 'fe/settle/**' --script check.py [--title …]
             (근거 기본 = 사람 명시 요청. 기계 사실이면 --basis machine_fact --command "<읽은 명령>")
    gate from-request --quote "<사람 원문>" [--session --area]   본체를 LLM이 쓰고 엔진이 검사
    gate list [--json]
    gate on <id>        [켜기]       gate keep <id>      [유지(기본)]
    gate delete <id>    사람의 "그 게이트 지워"·[삭제] = finding
    gate check-targets  대상 파일이 사라진 게이트 처리(세션 시작 때 자동)
    gate meta-check     작업 트리 L1 변경의 조이기/풀기 판정
    gate request protected_shrink|profile_loosen|prod_allow <항목…> --task T   안전 경계 넓히기 승인 카드
        (prod_allow 항목: names:<운영 아닌 서버·DB 이름> · files:<운영 접속 설정으로 보지 않을 경로 glob> — 승인 뒤 config.json prod_allow에 적는다)
    gate rules render | refresh   규칙 블록(AGENTS.md 표시 블록) 다시 쓰기·기계 사실 재확인
    gate codex-trust [--root]     이 PC에서 Codex 훅 신뢰 등록
만들 때 비용 한 줄을 알리고 묻지 않고 진행한다. 범위는 사람이 한 말 그대로 — 원문을 고치지 않는다.
"""

import argparse
import json
import sys
from pathlib import Path

from vmc_harness.core import paths
from vmc_harness.core.clock import now_iso


def register(sub):
    p = sub.add_parser("gate", help="생성 게이트 만들기·끄고 켜기·규칙 블록")
    s = p.add_subparsers(dest="action")
    a = s.add_parser("add", help="게이트 생성(사람 명시 요청·기계 사실)")
    a.add_argument("--title", required=True)
    a.add_argument("--basis", choices=("explicit_request", "machine_fact"), default="explicit_request")
    a.add_argument("--quote", help="사람 요청 원문 그대로")
    a.add_argument("--session", help="출처 세션 id")
    a.add_argument("--date", help="출처 날짜(기본 오늘)")
    a.add_argument("--area", help="출처 작업 영역")
    a.add_argument("--command", help="기계 사실: 읽은 명령")
    a.add_argument("--paths", nargs="*", default=[], help="발동 조건 경로 glob")
    a.add_argument("--events", nargs="*", default=[], help="발동 이벤트(PreToolUse·PostToolUse·Stop…)")
    a.add_argument("--sizes", nargs="*", default=[], help="작업 크기 small·mid")
    a.add_argument("--change-kinds", nargs="*", default=[], help="add·modify·delete")
    a.add_argument("--targets", nargs="*", default=[], help="게이트가 가리키는 파일")
    a.add_argument("--script", required=True, help="본체 .py 파일(- 이면 stdin)")
    a.add_argument("--human-criterion", type=int, choices=(1, 2, 3), help="사람을 부르는 게이트면 개입 기준")
    a = s.add_parser("from-request", help="사람 요청 원문으로 게이트 본체 생성(LLM)")
    a.add_argument("--quote", required=True)
    a.add_argument("--session")
    a.add_argument("--area")
    a = s.add_parser("list")
    a.add_argument("--json", action="store_true")
    for name in ("on", "keep", "delete"):
        a = s.add_parser(name)
        a.add_argument("gate_id")
    s.add_parser("check-targets")
    s.add_parser("meta-check")
    a = s.add_parser("request", help="보호 영역 줄이기·느슨한 프로파일·운영 판정 허용 칸 승인 카드(사람 개입 기준 2)")
    a.add_argument("kind", choices=("protected_shrink", "profile_loosen", "prod_allow"))
    a.add_argument("items", nargs="+", help="뺄 glob · 바꿀 프로파일 이름 · prod_allow는 names:<서버·DB 이름> 또는 files:<경로 glob>")
    a.add_argument("--task", required=True, help="이 요청을 시킨 작업 id")
    a.add_argument("--reason", help="이유 한 줄")
    a = s.add_parser("rules")
    a.add_argument("op", choices=("render", "refresh"))
    a = s.add_parser("codex-trust")
    a.add_argument("--root", help=argparse.SUPPRESS)
    p.set_defaults(func=run)


def run(args):
    root = Path(args.root) if getattr(args, "root", None) else paths.find_project_root()
    if root is None:
        print("하네스가 설치된 프로젝트가 아님(.harness/config.json 없음)", file=sys.stderr)
        return 2
    from vmc_harness.core import ledger
    from vmc_harness.gates import generated
    act = args.action
    try:
        if act == "add":
            text = sys.stdin.read() if args.script == "-" else Path(args.script).read_text(encoding="utf-8")
            if args.basis == "explicit_request":
                if not args.quote:
                    print("사람 요청 원문(--quote) 없음", file=sys.stderr)
                    return 2
                basis = {"type": "explicit_request", "quote": args.quote,
                         "source": {"session": args.session, "date": args.date or now_iso()[:10], "area": args.area}}
            else:
                if not args.command:
                    print("기계 사실 근거에 읽은 명령(--command) 없음", file=sys.stderr)
                    return 2
                basis = {"type": "machine_fact", "command": args.command}
            trigger = {k: v for k, v in (("paths", args.paths), ("events", args.events), ("sizes", args.sizes),
                                         ("change_kinds", args.change_kinds)) if v}
            r = generated.create(root, args.title, basis, trigger, text, targets=args.targets,
                                 human_criterion=args.human_criterion, calls_human=bool(args.human_criterion))
            print(r["cost_line"])
            print("게이트 %s (조치 %s)" % (r["meta"]["gate_id"], r["meta"]["action_id"]))
        elif act == "from-request":
            from vmc_harness.gates import generate
            res = generate.from_request(root, args.quote, {"session": args.session, "area": args.area,
                                                           "date": now_iso()[:10]})
            if not res:
                # 기계로 판정할 수 없는 요청 → 같은 원문으로 규칙(selfcheck 명시 요청과 같은 경로, 범위를 넓히지 않는다)
                from vmc_harness.ledger import selfcheck
                rid, a = selfcheck.add_rule(root, args.quote, {"session": args.session, "area": args.area,
                                                               "date": now_iso()[:10]},
                                            why_not_higher="기계로 막을 수 있는 형태가 아니어서 규칙으로 둔다")
                print("게이트로 만들 수 없는 요청 — 규칙으로 넣음 %s (조치 %s)" % (rid, a["action_id"]))
                return 0
            print(res["cost_line"])
            print("게이트 %s (조치 %s)" % (res["gate_id"], res["action_id"]))
        elif act == "list":
            gs = generated.load_all(root)
            if args.json:
                print(json.dumps(gs, ensure_ascii=False, indent=2))
            for g in gs if not args.json else []:
                state = "켜짐" if generated.active(g) else "꺼짐(%s)" % (g.get("disabled_reason") or g.get("status"))
                print("%s  %s  %s  %s  평균 %.0fms" % (g["gate_id"], g.get("title"), state,
                                                     (g.get("basis") or {}).get("type"),
                                                     float((g.get("cost") or {}).get("avg_ms") or 0)))
        elif act == "on":
            generated.enable(root, args.gate_id)
            print("켰어요: %s" % args.gate_id)
        elif act == "keep":
            generated.keep(root, args.gate_id)
            print("그대로 둬요: %s" % args.gate_id)
        elif act == "delete":
            r = generated.delete(root, args.gate_id)
            print("지웠어요: %s (%s)" % (args.gate_id, r["finding"]["finding_id"]))
        elif act == "check-targets":
            for gid, what, new in generated.check_targets(root):
                print("%s %s %s" % (gid, what, json.dumps(new, ensure_ascii=False) if new else ""))
        elif act == "meta-check":
            from vmc_harness.gates import meta
            v = meta.judge_worktree(root)
            print("\n".join(v) if v else "L1 변경 문제 없음")
            return 1 if v else 0
        elif act == "request":
            from vmc_harness.gates import meta
            a = meta.request_loosen(root, args.task, args.kind, args.items, args.reason)
            print("승인 카드 %s — 사람 승인 뒤 L1 변경 허용" % a["approval_id"])
        elif act == "rules":
            from vmc_harness.gates import rules
            if args.op == "render":
                r = rules.render(root)
                print("규칙 블록 %s (추정 %d토큰)" % ("갱신" if r["changed"] else "변경 없음", r["tokens"]))
            else:
                ch = rules.refresh_machine_facts(root, force=True)
                print("바뀐 기계 사실 규칙: %s" % (", ".join(ch) or "없음"))
        elif act == "codex-trust":
            from vmc_harness.hooks import install
            print(json.dumps(install.trust_codex(root), ensure_ascii=False))
        else:
            print(__doc__)
            return 2
    except (ledger.MetaGateError, ValueError, KeyError, OSError) as e:
        print("못 함: %s" % e, file=sys.stderr)
        return 1
    return 0
