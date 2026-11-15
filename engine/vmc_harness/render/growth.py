"""성장 브리핑·팀 대시보드 (spec 08 §브리핑 — 성장(주 1회, 월요일, 기계 집계)).

- 내 성장: 새로 배운 판단(한 줄씩·출처), 틀렸다가 고친 것(원복), 예측 적중 추이, 제안 수용률·막은 것·개입 추이(기준선 대비)
- 팀: 이번 주 팀 숫자, 확산(사용 인원·공유된 판단), 하네스가 한 일(추가·원복·정리), 보고용 한 줄(양 팀장님 말: 체계·표준·산출물·실체)
- 산출물: `.harness/cache/reports/growth-<YYYY-Www>.html`, `team-<YYYY-Www>.html` + `.pdf`(Chrome/Edge headless, 없으면 HTML만)
- 월요일 자동 생성: handlers.on_session_start → ensure_weekly()가 그 주 것이 없으면 백그라운드로 만든다

모두 기계 집계다(판정·LLM 없음). 보고용 한 줄만 렌더링 엔진 검사를 거친다.
"""

import datetime as _dt
import html
import os
import shutil
import sys
from pathlib import Path

from vmc_harness.core import config, ledger, paths
from vmc_harness.core.clock import now, parse_iso
from vmc_harness.core.jsonio import read_json

LEARN_LAYERS = ("card", "rule", "gate", "structure")
SOURCE_LABEL = {"explicit_request": "요청", "machine_fact": "코드에서 읽음", "learning": "대화에서 배움",
                "finding": "되돌림에서", "cleansing": "정리", "diagnosis_auto": "진단 자동 반영"}


# ---------------- 기간 ----------------

def week_start(d=None):
    d = (d or now()).astimezone()
    s = d - _dt.timedelta(days=d.weekday())
    return s.replace(hour=0, minute=0, second=0, microsecond=0)


def week_key(start):
    y, w, _ = start.isocalendar()
    return "%d-W%02d" % (y, w)


def week_label(start):
    end = start + _dt.timedelta(days=4)
    return "%d/%d ~ %d/%d" % (start.month, start.day, end.month, end.day)


def _in(at, start, end):
    d = parse_iso(at) if isinstance(at, str) else at
    if not d:
        return False
    if d.tzinfo is None:
        d = d.astimezone()
    return start <= d < end


def _date_in(date_str, start, end):
    return start.strftime("%Y-%m-%d") <= (date_str or "") < end.strftime("%Y-%m-%d")


# ---------------- 재료 ----------------

def viewers(root):
    """이 PC 사람을 가리키는 이름들(core.project.author_name — 프로젝트 기준 + 프로젝트 밖에서 만든 카드용 전역 기준)."""
    from vmc_harness.core.project import author_name
    return {n for n in (author_name(root), author_name(None)) if n}


def _me(root):
    from vmc_harness.render.profile import git_user
    return git_user(root) or next(iter(viewers(root)), None)


def _reverted_at(a, verdicts):
    v = verdicts.get(a.get("action_id")) or {}
    if a.get("status") != "reverted" and v.get("result") != "reverted":
        return None
    return a.get("reverted_at") or v.get("at") or ((v.get("period") or [None, None])[1]) or a.get("updated_at")


def learned(root, start, end, me=None, actions=None):
    """새로 배운 판단 한 줄씩·출처. 작성자는 본인일 때만(다른 팀원 화면에선 뺀다 — 12 결정)."""
    actions = ledger.list_actions(root) if actions is None else actions
    out = []
    for a in actions:
        if not a or a.get("layer") not in LEARN_LAYERS or a.get("status") == "reverted":
            continue
        if not _in(a.get("created_at"), start, end):
            continue
        basis = a.get("basis") or {}
        item = {"action_id": a["action_id"], "text": a.get("title") or a.get("summary") or a.get("hypothesis") or "",
                "layer": a.get("layer"), "source": SOURCE_LABEL.get(basis.get("type"), basis.get("type") or ""),
                "at": a.get("created_at"), "buttons": ["삭제"]}
        author = a.get("author")
        mine = me if isinstance(me, (set, frozenset, list, tuple)) else {me}
        if author and author in mine:
            item["author"] = author
        out.append(item)
    return out


def fixed(root, start, end, actions=None, verdicts=None):
    """틀렸다가 고친 것(원복된 하네스 조치)."""
    actions = ledger.list_actions(root) if actions is None else actions
    vmap = {v.get("action_id"): v for v in (ledger.list_verdicts(root) if verdicts is None else verdicts) if v}
    out = []
    for a in actions:
        if not a:
            continue
        at = _reverted_at(a, vmap)
        if at and _in(at, start, end):
            v = vmap.get(a["action_id"]) or {}
            out.append({"action_id": a["action_id"], "text": a.get("title") or a.get("hypothesis") or "",
                        "reason": v.get("reason") or a.get("revert_reason"), "at": at})
    return out


def prediction_hit(tasks, start, end):
    """예측 적중 = 실측 에이전트 시간이 선언한 P30~P80 안. 측정된 작업만(미측정은 빼고 센다)."""
    n = hit = 0
    for t in tasks:
        if not _in(t.get("started_at"), start, end):
            continue
        act = t.get("actual") or {}
        pred = ((t.get("declare") or {}).get("predict") or {}).get("agent_sec") or {}
        if not act.get("measured") or not pred.get("p80"):
            continue
        n += 1
        a = float(act.get("agent_sec") or 0)
        if float(pred.get("p30") or 0) <= a <= float(pred["p80"]):
            hit += 1
    return {"tasks": n, "rate": round(hit / n, 2) if n else None}


def _metrics(root, start, end):
    try:
        from vmc_harness.ledger import metrics
        rows = metrics.load_all(root, since=start.strftime("%Y-%m-%d"))
    except ImportError:
        return []
    return [m for m in rows if _date_in(m.get("date"), start, end)]


def blocked(root, start, end, rows=None):
    rows = _metrics(root, start, end) if rows is None else rows
    by = {}
    for m in rows:
        for gid, g in (m.get("gates") or {}).items():
            x = by.setdefault(gid, {"blocked": 0, "false_block": 0})
            x["blocked"] += int(g.get("blocked") or 0)
            x["false_block"] += int(g.get("false_block") or 0)
    total = sum(x["blocked"] for x in by.values())
    top = max(by.items(), key=lambda kv: kv[1]["blocked"])[0] if total else None
    return {"total": total, "false_block": sum(x["false_block"] for x in by.values()), "by_gate": by, "top_gate": top}


def interventions(root, start, end, rows=None):
    """세션당 사람 개입(참고 지표) — 기준선 대비."""
    rows = _metrics(root, start, end) if rows is None else rows
    s = sum(int(m.get("sessions") or 0) - int(m.get("unmeasured_sessions") or 0) for m in rows)
    h = sum(int(m.get("human_turns") or 0) for m in rows)
    base = None
    b = read_json(ledger.baseline_path(root), None)
    if isinstance(b, dict):
        base = b.get("human_turns_per_session") or (b.get("interventions") or {}).get("per_session")
    return {"per_session": round(h / s, 2) if s else None, "baseline": base, "sessions": s}


def acceptance(root, start, end):
    """제안 수용률 = 그 기간에 보인 진단 제안 중 [추가]한 비율(11 회귀 세트 ⑤). 진단 기록이 없으면 None."""
    try:
        from vmc_harness.bootstrap import diagnose
        hist = read_json(diagnose.history_path(root), None)
    except Exception:
        return None
    if not isinstance(hist, dict):
        return None
    shown = sum(1 for h in hist.get("shown") or [] if isinstance(h, dict) and not h.get("ticket") and not h.get("auto")
                and _in(h.get("at"), start, end))  # 루프 티켓(자동 등록)·자동 반영은 사람이 고를 제안이 아니다
    added = sum(1 for h in hist.get("accepted") or [] if isinstance(h, dict) and _in(h.get("at"), start, end))
    if not shown and not added:
        return None
    return {"shown": shown, "added": added, "rate": round(added / shown, 2) if shown else None}


def _gate_name(root, gid):
    if not gid:
        return None
    g = read_json(paths.harness_dir(root) / "gates" / ("%s.json" % gid), None) or {}
    return g.get("title") or g.get("name") or gid


def project_week_no(root, start):
    cfg = read_json(paths.harness_dir(root) / "config.json", {}) or {}
    c = parse_iso(cfg.get("created_at"))
    if not c:
        return 1
    return max(1, (start - week_start(c)).days // 7 + 1)


# ---------------- 팀 ----------------

def team(root, start=None, end=None):
    """팀 대시보드 데이터(모두 같은 화면). 기간 기본 = 이번 주."""
    start = start or week_start()
    end = end or start + _dt.timedelta(days=7)
    tasks = ledger.read_tasks(root)
    actions = [a for a in ledger.list_actions(root) if a]
    verdicts = [v for v in ledger.list_verdicts(root) if v]
    rows = _metrics(root, start, end)
    b = blocked(root, start, end, rows)
    iv = interventions(root, start, end, rows)
    users_week = sorted({t.get("pc_id") for t in tasks if t.get("pc_id") and _in(t.get("started_at"), start, end)})
    users_all = sorted({t.get("pc_id") for t in tasks if t.get("pc_id")})
    names = sorted({t.get("user") or t.get("author") for t in tasks
                    if (t.get("user") or t.get("author")) and _in(t.get("started_at"), start, end)})
    shared = [a for a in actions if a.get("layer") in ("card", "rule") and a.get("status") in ("trial", "kept")]
    added = [a for a in actions if _in(a.get("created_at"), start, end)]
    reverted = fixed(root, start, end, actions, verdicts)
    cleaned = [a for a in actions if a.get("layer") == "code" and a.get("merged_at")
               and not a.get("discarded") and _in(a.get("merged_at") or a.get("updated_at") or a.get("created_at"), start, end)]
    numbers = [{"key": "blocked", "label": "사전 차단", "value": "%d건" % b["total"],
                "note": _gate_name(root, b["top_gate"]) and "%s 등" % _gate_name(root, b["top_gate"])}]
    if iv["per_session"] is not None:
        val = ("%s → %s" % (iv["baseline"], iv["per_session"])) if iv["baseline"] is not None else str(iv["per_session"])
        numbers.append({"key": "interventions", "label": "세션당 사람 개입", "value": val, "note": "참고 지표"})
    try:
        from vmc_harness.render import approval_card
        slow = [s for s in approval_card.slow_types(root, since=start.isoformat()) if s["slow"]]
    except Exception:
        slow = []
    if slow:
        numbers.append({"key": "slow_approvals", "label": "3분 넘는 확인", "value": "%d종" % len(slow),
                        "note": "참고 지표 · " + ", ".join(s["type"] for s in slow)})
    out = {
        "week": week_label(start), "week_key": week_key(start), "week_start": start.isoformat(),
        "numbers": {"rows": numbers, "blocked": b["total"], "false_block": b["false_block"],
                    "interventions": iv},
        "spread": {"users": len(users_week), "users_total": len(users_all), "names": names,
                   "shared_judgments": len(shared)},
        "harness_did": {"added": len(added), "reverted": len(reverted), "cleaned": len(cleaned),
                        "items": [{"action_id": a["action_id"], "layer": a.get("layer"),
                                   "text": a.get("title") or a.get("hypothesis") or ""} for a in added]},
    }
    out["report_line"] = report_line(root, out, project_week_no(root, start))
    return out


def report_line(root, t, week_no):
    """보고용 한 줄 — 양 팀장님 말(체계·표준·산출물·실체)로. 숫자는 기계 집계 그대로."""
    parts = []
    if t["numbers"]["blocked"]:
        parts.append("사전 차단 %d건" % t["numbers"]["blocked"])
    if t["spread"]["shared_judgments"]:
        parts.append("공유된 판단 %d개(산출물)" % t["spread"]["shared_judgments"])
    u, total = t["spread"]["users"], t["spread"]["users_total"]
    if u:
        parts.append(("%d명 모두 사용" % u) if total and u == total else "%d명 사용" % u)
    if t["harness_did"]["added"] or t["harness_did"]["reverted"]:
        parts.append("하네스 추가 %d · 원복 %d" % (t["harness_did"]["added"], t["harness_did"]["reverted"]))
    line = "AI 개발 표준 체계 적용 %d주차" % week_no
    if parts:
        line += " — " + ", ".join(parts)
    try:
        from vmc_harness.render import engine
        facts = {"week_no": week_no, "blocked": t["numbers"]["blocked"], "users": u,
                 "shared": t["spread"]["shared_judgments"]}
        r = engine.render(root, "team", facts, draft=line, kind="report_line", allow_llm=False)
        return r["text"]
    except Exception:
        return line


# ---------------- 내 성장 ----------------

def me(root, start, end, viewer=None):
    viewer = viewer if viewer is not None else viewers(root)
    tasks = ledger.read_tasks(root)
    trend = []
    n_weeks = config.get("render.trend_weeks", root)
    for i in range(n_weeks - 1, -1, -1):
        s = start - _dt.timedelta(days=7 * i)
        trend.append(dict(prediction_hit(tasks, s, s + _dt.timedelta(days=7)), week=week_label(s)))
    rows = _metrics(root, start, end)
    return {
        "learned": learned(root, start, end, viewer),
        "fixed": fixed(root, start, end),
        "prediction_trend": trend,
        "acceptance": acceptance(root, start, end),
        "blocked": blocked(root, start, end, rows),
        "interventions": interventions(root, start, end, rows),
        "predict_hint": _predict_hint(tasks, start, end),
    }


def _predict_hint(tasks, start, end):
    """예측 회고의 사용자 행동 한 줄(spec 13 — 사람에게 할 일을 주는 건 주 1회 이 한 줄뿐)."""
    try:
        from vmc_harness.task.analysis import weekly_hint
        return weekly_hint(tasks, start, end)
    except Exception:
        return None


def weekly(root, ref=None):
    """월요일 성장 브리핑 재료 — 지난주(월~일) 집계."""
    this = week_start(ref)
    start = this - _dt.timedelta(days=7)
    try:
        from vmc_harness.render import approval_card
        slow = approval_card.slow_types(root, since=start.isoformat())
    except Exception:
        slow = []
    return {"week": week_label(start), "week_key": week_key(start), "me": me(root, start, this),
            "team": team(root, start, this), "slow_approvals": slow}


# ---------------- HTML·PDF ----------------

_CSS = """body{font-family:-apple-system,"Pretendard","Malgun Gothic",sans-serif;max-width:860px;margin:32px auto;padding:0 20px;color:#222;line-height:1.6}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px;border-bottom:1px solid #ddd;padding-bottom:4px}
.dim{color:#777}.row{display:flex;justify-content:space-between;border-bottom:1px dotted #eee;padding:4px 0}
.big{font-weight:700}.line{border:1px solid #ccc;border-radius:6px;padding:10px 12px;background:#faf9f6}
ul{padding-left:18px}li{margin:2px 0}"""


def _e(s):
    return html.escape(str(s if s is not None else ""))


def _team_html(t):
    rows = "".join('<div class="row"><span>%s</span><span class="big">%s</span><span class="dim">%s</span></div>'
                   % (_e(r["label"]), _e(r["value"]), _e(r.get("note") or "")) for r in t["numbers"]["rows"])
    sp = t["spread"]
    hd = t["harness_did"]
    return ("<h2>이번 주 팀 숫자</h2>%s"
            "<h2>확산</h2><div class='row'><span>사용 인원</span><span class='big'>%d / %d</span><span class='dim'>%s</span></div>"
            "<div class='row'><span>공유된 판단</span><span class='big'>%d개</span><span class='dim'>한 사람만 알던 규칙</span></div>"
            "<h2>하네스가 한 일</h2><p>추가 %d · 원복 %d · 정리 %d</p>"
            "<h2>보고용 한 줄</h2><div class='line'>%s</div>") % (
        rows, sp["users"], sp["users_total"], _e(" · ".join(sp["names"])), sp["shared_judgments"],
        hd["added"], hd["reverted"], hd["cleaned"], _e(t["report_line"]))


def _pct(x):
    return "-" if x is None else "%d%%" % round(x * 100)


def growth_html(data):
    m = data["me"]
    learned_li = "".join("<li>%s <span class='dim'>· %s%s</span></li>" % (
        _e(x["text"]), _e(x["source"]), (" · " + _e(x["author"])) if x.get("author") else "") for x in m["learned"]) or "<li class='dim'>없음</li>"
    fixed_li = "".join("<li>%s <span class='dim'>%s</span></li>" % (_e(x["text"]), _e(x.get("reason") or ""))
                       for x in m["fixed"]) or "<li class='dim'>없음</li>"
    trend = " → ".join("%s(%s)" % (_pct(t["rate"]), t["week"].split(" ")[0]) for t in m["prediction_trend"])
    acc = m["acceptance"]
    iv = m["interventions"]
    slow = "".join("<li>%s · 중앙 %s초 · %d건%s</li>" % (_e(s["type"]), s["median_sec"], s["count"],
                                                     " <b>(3분 넘음)</b>" if s["slow"] else "")
                   for s in data.get("slow_approvals") or []) or "<li class='dim'>없음</li>"
    body = ("<h1>하네스 성장 브리핑</h1><p class='dim'>%s · 기계 집계</p>"
            "<h2>새로 배운 판단</h2><ul>%s</ul><h2>틀렸다가 고친 것(원복)</h2><ul>%s</ul>"
            "<h2>예측 적중 추이</h2><p>%s</p>%s"
            "<h2>제안 수용률 · 막은 것 · 개입</h2>"
            "<div class='row'><span>제안 수용률</span><span class='big'>%s</span><span class='dim'>%s</span></div>"
            "<div class='row'><span>막은 것</span><span class='big'>%d건</span><span class='dim'>헛막기 %d</span></div>"
            "<div class='row'><span>세션당 사람 개입</span><span class='big'>%s</span><span class='dim'>기준선 %s · 참고 지표</span></div>"
            "<h2>확인 응답 시간(참고 지표)</h2><ul>%s</ul>") % (
        _e(data["week"]), learned_li, fixed_li, _e(trend),
        ("<p class='dim'>%s</p>" % _e(m["predict_hint"])) if m.get("predict_hint") else "",
        _pct(acc["rate"]) if acc else "-", ("%d개 중 %d개 추가" % (acc["shown"], acc["added"])) if acc else "진단 제안 없음",
        m["blocked"]["total"], m["blocked"]["false_block"],
        _e(iv["per_session"] if iv["per_session"] is not None else "-"), _e(iv["baseline"] if iv["baseline"] is not None else "-"),
        slow)
    body += "<h1 style='margin-top:40px'>팀 대시보드</h1>" + _team_html(data["team"])
    return "<!doctype html><html lang='ko'><head><meta charset='utf-8'><title>하네스 성장 브리핑 %s</title><style>%s</style></head><body>%s</body></html>" % (
        _e(data["week"]), _CSS, body)


def team_html(t):
    return ("<!doctype html><html lang='ko'><head><meta charset='utf-8'><title>팀 대시보드 %s</title><style>%s</style></head>"
            "<body><h1>팀 대시보드</h1><p class='dim'>%s · 기계 집계</p>%s</body></html>") % (
        _e(t["week"]), _CSS, _e(t["week"]), _team_html(t))


def reports_dir(root):
    return paths.ensure(paths.cache_dir(root) / "reports")


def find_browser():
    """Chrome/Edge/Chromium 실행 파일. 없으면 None."""
    env = os.environ.get("VMC_HARNESS_BROWSER")
    if env:
        return env if Path(env).exists() else None
    cands = []
    if sys.platform == "darwin":
        cands += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                  "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    elif sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
            if base:
                cands += [str(Path(base) / "Google/Chrome/Application/chrome.exe"),
                          str(Path(base) / "Microsoft/Edge/Application/msedge.exe")]
    for c in cands:
        if Path(c).exists():
            return c
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "msedge", "chrome"):
        w = shutil.which(name)
        if w:
            return w
    return None


def to_pdf(root, html_path):
    """HTML → PDF(브라우저 headless). 브라우저가 없거나 실패하면 None(HTML만)."""
    exe = find_browser()
    if not exe:
        return None
    from vmc_harness.core.proc import run
    html_path = Path(html_path)
    pdf = html_path.with_suffix(".pdf")
    rc, _, _ = run([exe, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                    "--print-to-pdf=%s" % pdf, html_path.resolve().as_uri()],
                   timeout=config.get("render.pdf_timeout_s", root))
    return pdf if pdf.is_file() and pdf.stat().st_size > 0 else None


def write_team(root, t=None, pdf=True):
    t = t or team(root)
    p = reports_dir(root) / ("team-%s.html" % t["week_key"])
    p.write_text(team_html(t), encoding="utf-8")
    return {"html": str(p), "pdf": str(to_pdf(root, p) or "") or None}


def generate_weekly(root, ref=None, pdf=True):
    data = weekly(root, ref)
    d = reports_dir(root)
    g = d / ("growth-%s.html" % data["week_key"])
    g.write_text(growth_html(data), encoding="utf-8")
    tp = d / ("team-%s.html" % data["week_key"])
    tp.write_text(team_html(data["team"]), encoding="utf-8")
    tpdf = to_pdf(root, tp) if pdf else None
    return {"growth_html": str(g), "team_html": str(tp), "team_pdf": str(tpdf) if tpdf else None, "week": data["week"]}


def weekly_path(root, ref=None):
    start = week_start(ref) - _dt.timedelta(days=7)
    return paths.cache_dir(root) / "reports" / ("growth-%s.html" % week_key(start))


def ensure_weekly(root, spawn=True):
    """이번 주(월요일부터) 성장 브리핑이 없으면 백그라운드로 만든다. 만들기 시작했으면 True."""
    if weekly_path(root).is_file():
        return False
    if not spawn:
        generate_weekly(root)
        return True
    from vmc_harness.core.proc import spawn_detached
    spawn_detached([sys.executable, "-m", "vmc_harness", "brief", "--weekly"], cwd=str(root))
    return True
