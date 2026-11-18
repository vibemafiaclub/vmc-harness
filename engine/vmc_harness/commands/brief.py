"""`vmc-harness brief` — 브리핑(spec 08). mod 패널은 `--json`을 그리기만 한다. mod 없이도 글자·HTML로 똑같이 본다.

  brief                 내 작업 탭(글자). 보인 알림은 다시 알리지 않는다
  brief --tab team      팀 대시보드 탭
  brief --json          09 브리핑 스키마(mod 입력). 재작성이 남았으면 백그라운드로 다듬는다
  brief --html          이번 주 HTML(.harness/cache/reports/) 경로 출력  ·  --pdf  팀 대시보드 PDF
  brief --refresh       캐시 갱신(렌더링 재작성 포함) — 세션 시작 훅이 백그라운드로 부른다
  brief --weekly        지난주 성장 브리핑 HTML + 팀 대시보드 HTML·PDF 생성(월요일 자동)
  brief --top [--json] [--mark-seen]  사람이 답할 만한 가장 중요한 1건(매일 바깥 브리핑용). --mark-seen은 실제로 보냈을 때만
"""

import json
import sys


def register(sub):
    p = sub.add_parser("brief", help="브리핑: 내 작업·팀 대시보드 (글자 | --json | --html)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--html", action="store_true")
    p.add_argument("--pdf", action="store_true", help="팀 대시보드 PDF(브라우저 headless, 없으면 HTML만)")
    p.add_argument("--tab", choices=("me", "team"), default="me")
    p.add_argument("--all", action="store_true", help="칸마다 전부(더 보기)")
    p.add_argument("--session", help="세션 id(mod가 넘긴다)")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--weekly", action="store_true")
    p.add_argument("--top", action="store_true", help="가장 중요한 1건(바깥 브리핑용)")
    p.add_argument("--mark-seen", action="store_true", help="--top으로 고른 알림을 보인 것으로 표시")
    p.set_defaults(func=_run)


def _root():
    from vmc_harness.core import paths
    return paths.find_project_root()


def _run(a):
    root = _root()
    if not root:
        if a.json:
            print(json.dumps({"installed": False}, ensure_ascii=False))
            return 0
        print("하네스가 설치되지 않은 프로젝트")
        return 2
    from vmc_harness.render import brief, growth
    if a.top:
        item = brief.top_item(root, mark=a.mark_seen)
        if a.json:
            print(json.dumps({"installed": True, "item": item, "text": brief.top_text(item)}, ensure_ascii=False))
        elif item:
            print(brief.top_text(item))
        return 0
    if a.refresh or a.weekly:
        out = {}
        if a.refresh:
            brief.build(root, session_id=a.session, allow_llm=True)
            out["brief"] = "ok"
        if a.weekly:
            out.update(growth.generate_weekly(root))
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if a.pdf:
        print(json.dumps(growth.write_team(root), ensure_ascii=False))
        return 0
    if a.html:
        import datetime as _dt
        ws = growth.week_start()
        data = {"week": growth.week_label(ws), "week_key": growth.week_key(ws),
                "me": growth.me(root, ws, ws + _dt.timedelta(days=7)), "team": growth.team(root, ws),
                "slow_approvals": []}
        p = growth.reports_dir(root) / ("brief-%s.html" % data["week_key"])
        p.write_text(growth.growth_html(data), encoding="utf-8")
        print(str(p))
        return 0
    b = brief.build(root, session_id=a.session, allow_llm=False)
    if a.json:
        b["installed"] = True
        print(json.dumps(b, ensure_ascii=False))
        if b.get("pending_render"):
            from vmc_harness.core.proc import spawn_detached
            try:
                spawn_detached([sys.executable, "-m", "vmc_harness", "brief", "--refresh"], cwd=str(root))
            except Exception:
                pass
        return 0
    print(brief.text(b, tab=a.tab, all_items=a.all))
    if a.tab == "me":
        per = b["ui"]["per_section"]
        shown = [n["id"] for n in (b["me"]["notices"] if a.all else b["me"]["notices"][:per]) if not n["seen"]]
        brief.mark_seen(root, shown)
        from vmc_harness.render import approval_card
        for c in (b["me"]["approvals"] if a.all else b["me"]["approvals"][:per]):
            approval_card.mark_shown(root, c["approval_id"])
    return 0
