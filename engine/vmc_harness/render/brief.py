"""브리핑 (spec 08 §브리핑, 09 brief.json) — mod 패널·`vmc-harness brief`·HTML의 공통 입력.

- 꼭 알릴 일(기계 조건, notices level=must) · 흐름(모델 판단, level=flow) · 성장(주 1회 기계 집계, growth.py)
- **세션 시작에 띄우지 않고, 작업 중 끼어들지 않고, 모델에게 주입하지 않는다.** 이 모듈은 아무것도 주입하지 않는다
- 사람마다 거른다(그 사람 작업 영역과 관계있는 것만) / 한 번 보인 건 다시 안 보인다 / 읽음 확인 없음
- 작업 관련 꼭 알릴 일: inline_notices(root, area) — task 영역 report가 완료 보고 블록에 싣는다
- Codex·mod 없는 환경: daily_two_lines(root) — 완료 보고 아래 하루 1회 2줄 + `vmc-harness brief`
- 패널이 다시 그려도 칸이 비지 않게, 본 알림은 render.notice_keep_hours 동안 `seen: true`로 남긴다(표시만, 다시 알리지 않음)
"""

import datetime as _dt

from vmc_harness import KERNEL_VERSION
from vmc_harness.core import config, ledger, notices, paths
from vmc_harness.core.clock import now, now_iso, parse_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.render import checks

TABS = ["내 작업", "팀 대시보드"]
EVIDENCE_LABEL = {"screenshot": "스크린샷", "api_call": "API 호출", "db_dryrun": "DB 드라이런",
                  "test_run": "테스트", "qa_scenario": "QA 시나리오"}
LEVELS = ("must", "flow")


# ---------------- 로컬 상태(PC 안) ----------------

def _state_path(root):
    from vmc_harness.core.project import local_dir
    return local_dir(root) / "brief-state.json"


def _state(root):
    return read_json(_state_path(root), {}) or {}


def _save_state(root, st):
    write_json(_state_path(root), st)


def mark_seen(root, ids):
    """보인 알림 — 다시 알리지 않는다(core.notices.mark_shown) + 패널 유지용 본 시각."""
    ids = [i for i in ids if i]
    if not ids:
        return
    notices.mark_shown(root, ids)
    st = _state(root)
    seen = st.setdefault("seen", {})
    t = now_iso()
    for i in ids:
        seen.setdefault(i, t)
    keep = config.get("render.notice_keep_hours", root) * 3600
    cutoff = now().timestamp() - keep * 4
    st["seen"] = {k: v for k, v in seen.items() if (parse_iso(v) and parse_iso(v).timestamp() >= cutoff)}
    _save_state(root, st)


# ---------------- 재료 ----------------

def _task_state():
    try:
        from vmc_harness.task import state
        return state
    except ImportError:
        return None


def current_task(root, session_id=None):
    """지금 세션의 작업(읽기만 — 세션 묶기 같은 부수 효과 없음)."""
    st = _task_state()
    if not st:
        return None
    try:
        sid = st.resolve_session_id(root, session_id) if hasattr(st, "resolve_session_id") else session_id
        tasks = st.open_tasks(root) if hasattr(st, "open_tasks") else []
    except Exception:
        return None
    mine = [t for t in tasks if sid and t.get("session_id") == sid]
    if mine:
        return mine[-1]
    unbound = [t for t in tasks if not t.get("session_id")]
    return unbound[-1] if unbound else None


def my_areas(root, cur=None):
    """이 사람이 최근 맡은 영역(선언 범위). 모르면 빈 목록 = 거르지 않는다."""
    st = _task_state()
    areas = set()
    if cur:
        areas.update((cur.get("declare") or {}).get("scope") or [])
    if st and hasattr(st, "all_tasks"):
        days = config.get("render.recent_area_days", root)
        cutoff = now() - _dt.timedelta(days=days)
        try:
            for t in st.all_tasks(root, archived=True):
                d = parse_iso(t.get("started_at"))
                if d and d >= cutoff:
                    areas.update((t.get("declare") or {}).get("scope") or [])
        except Exception:
            pass
    return sorted(a for a in areas if a)


def related(area, other):
    if not area or not other:
        return False
    a, b = area.strip("/"), other.strip("/")
    return a == b or a.startswith(b + "/") or b.startswith(a + "/") or a.startswith(b) or b.startswith(a)


def _minutes(sec):
    return max(1, int(round(float(sec) / 60.0)))


def _when(at):
    d = parse_iso(at)
    if not d:
        return ""
    d = d.astimezone()
    today = now().date()
    if d.date() == today:
        return "오늘 %02d:%02d" % (d.hour, d.minute)
    if d.date() == today - _dt.timedelta(days=1):
        return "어제"
    return "%d/%d" % (d.month, d.day)


def task_view(root, t):
    if not t:
        return None
    dec = t.get("declare") or {}
    pred = (dec.get("predict") or {}).get("agent_sec") or {}
    expect = None
    if pred.get("p50") and pred.get("p80"):
        expect = "%d~%d분" % (_minutes(pred["p50"]), _minutes(pred["p80"]))
    started = parse_iso(t.get("started_at"))
    elapsed = int((now() - started).total_seconds() // 60) if started else None
    ev = []
    try:
        from vmc_harness.gates import evidence
        for m in evidence.list_markers(root, t["task_id"]):
            f = m.get("file")
            path = str(evidence.evidence_dir(root, t["task_id"]) / f) if f else None
            ok = evidence.marker_ok(m) if hasattr(evidence, "marker_ok") else True
            ev.append({"kind": m.get("kind"), "label": "%s %s" % (EVIDENCE_LABEL.get(m.get("kind"), m.get("kind")),
                                                                 m.get("target") or ""),
                       "ok": ok, "file": path, "is_image": m.get("kind") == "screenshot"})
    except ImportError:
        pass
    oos = [{"path": x.get("path"), "reason": x.get("reason")} if isinstance(x, dict) else {"path": str(x), "reason": None}
           for x in (t.get("out_of_scope") or [])]
    return {"task_id": t["task_id"], "title": dec.get("intent") or "", "expect": expect, "elapsed_min": elapsed,
            "evidence": ev, "evidence_ok": (all(e["ok"] for e in ev) if ev else None),
            "out_of_scope": oos, "buttons": ["자세히"] if oos else []}


def _facts_from_text(text):
    keep = [tok for tok in checks.tokens(text) if checks._is_name_like(tok)]
    return {"_keep": sorted(set(keep))}


def _render_text(root, text, kind, allow_llm):
    from vmc_harness.render import engine
    r = engine.render(root, "me", _facts_from_text(text), draft=text, kind=kind, allow_llm=allow_llm, cache=True)
    return r["text"], engine._passed(r["checks"]) or bool(r.get("llm_calls")) or bool(r.get("cached"))


def notices_view(root, areas, allow_llm=False):
    """→ (보일 알림 목록, 재작성이 남았나)"""
    st = _state(root)
    seen = st.get("seen") or {}
    keep_s = config.get("render.notice_keep_hours", root) * 3600
    pending_ids = {n["id"] for n in notices.pending(root)}
    from vmc_harness.core.project import local_dir
    from vmc_harness.core.jsonio import read_jsonl
    allnotes = read_jsonl(local_dir(root) / "notices.jsonl")
    muted = set((read_json(local_dir(root) / "notices-state.json", {}) or {}).get("muted_kinds") or [])
    out, todo = [], False
    for n in allnotes:
        if n.get("level") not in LEVELS or n.get("kind") in muted:
            continue
        is_pending = n["id"] in pending_ids
        sa = parse_iso(seen.get(n["id"])) if seen.get(n["id"]) else None
        recent_seen = sa is not None and (now() - sa).total_seconds() <= keep_s
        if not (is_pending or recent_seen):
            continue
        if areas and n.get("area") and not any(related(n["area"], a) for a in areas):
            continue
        text, done = _render_text(root, n.get("text") or "", "notice", allow_llm)
        todo = todo or not done
        out.append({"id": n["id"], "kind": n.get("kind"), "level": n.get("level"), "text": text,
                    "area": n.get("area"), "when": _when(n.get("at")), "at": n.get("at"),
                    "refs": n.get("refs") or {}, "buttons": n.get("buttons") or ["자세히", "알림 끄기"],
                    "seen": not is_pending, "raw": n.get("text") or ""})
    out.sort(key=lambda x: (x["seen"], 0 if x["level"] == "must" else 1, x.get("at") or ""), reverse=False)
    return out, todo


def approvals_view(root, cur, allow_llm=False):
    """사람이 시킨 일 안의 승인 카드만(01 P4): 지금 작업 것 + 이 PC 작업 것."""
    from vmc_harness.render import approval_card
    st = _task_state()
    cur_id = cur.get("task_id") if cur else None
    out, todo = [], False
    for rec in ledger.list_approvals(root):
        if not rec or rec.get("answer"):
            continue
        tid = rec.get("task_id")
        mine = tid == cur_id or (st is not None and hasattr(st, "by_id") and st.by_id(root, tid))
        if not mine:
            continue
        try:
            c = approval_card.card(root, rec, allow_llm=allow_llm)
        except approval_card.CardError:
            continue
        todo = todo or not c.get("final")
        out.append(c)
    return out, todo


def proposals_view(root):
    """진단 제안 [추가]/[제외] — 아직 답하지 않은 것(bootstrap.diagnose 결과). id = 제안 번호."""
    try:
        from vmc_harness.bootstrap import diagnose
        d = diagnose.load(root) or {}
    except Exception:
        return []
    out = []
    for p in d.get("proposals") or []:
        if not isinstance(p, dict) or p.get("no") is None or p.get("accepted") or p.get("excluded"):
            continue
        text = ((p.get("display") or {}).get("rendered") or p.get("title") or "").splitlines()
        line = text[0] if text else ""
        aid = (p.get("auto_applied") or {}).get("action_id")
        if aid:  # 진단 자동 반영 — 시험 중인 동안만 보이고 [제외] = 되돌리기(사후 원클릭)
            if (ledger.get_action(root, aid) or {}).get("status") in ("trial", "extended"):
                out.append({"id": str(p["no"]), "text": "자동 반영(시험 중): " + line, "buttons": ["제외"]})
            continue
        if p.get("ticket_id"):  # 루프 티켓으로 자동 등록 — 아직 루프가 손대기 전에만 보이고 [제외]로 취소
            c = ledger.get_candidate(root, p["ticket_id"]) or {}
            if c.get("status") != "open":
                continue
            out.append({"id": str(p["no"]), "text": "루프가 처리할 일: " + line, "buttons": ["제외"]})
            continue
        out.append({"id": str(p["no"]), "text": line, "buttons": ["추가", "제외"]})
    return out


def gates_active(root):
    d = paths.harness_dir(root) / "gates"
    n = 0
    if d.is_dir():
        for f in d.glob("*.json"):
            g = read_json(f, None) or {}
            if g.get("enabled") is False or g.get("status") in ("disabled", "off", "reverted"):
                continue
            n += 1
    return n


def _auto_update():
    """엔진 자동 업데이트 켜짐 여부(dist, PC 단위). 못 읽으면 None — 상태 줄에 안 보인다."""
    try:
        from vmc_harness.dist import updater
        return updater.auto_enabled()
    except Exception:
        return None


def status_line(st):
    ev = {True: "✓", False: "✗", None: "-"}[st["evidence"]]
    line = "🛡 %d · 증거 %s · 확인 %d · 알림 %d" % (st["gates"], ev, st["approvals"], st["notices"])
    return line + (" · 자동 업데이트 꺼짐" if st.get("auto_update") is False else "")


# ---------------- 빌드 ----------------

def build(root, session_id=None, allow_llm=False, write=True):
    """09 브리핑 스키마 dict. allow_llm=False(기본)면 검사만 하고 재작성은 백그라운드 refresh가 한다."""
    from vmc_harness.render import growth
    cur = current_task(root, session_id)
    tv = task_view(root, cur)
    areas = my_areas(root, cur)
    nv, todo_n = notices_view(root, areas, allow_llm)
    av, todo_a = approvals_view(root, cur, allow_llm)
    ws = growth.week_start()
    viewer = growth._me(root)
    learned = growth.learned(root, ws, ws + _dt.timedelta(days=7), growth.viewers(root))
    fixed = growth.fixed(root, ws, ws + _dt.timedelta(days=7))
    tasks = ledger.read_tasks(root)
    prev = growth.prediction_hit(tasks, ws - _dt.timedelta(days=7), ws)
    this = growth.prediction_hit(tasks, ws, ws + _dt.timedelta(days=7))
    team = growth.team(root, ws)
    st = {"gates": gates_active(root), "evidence": tv["evidence_ok"] if tv else None,
          "approvals": len(av), "notices": sum(1 for n in nv if not n["seen"]),
          "auto_update": _auto_update()}
    b = {
        "generated_at": now_iso(), "kernel_version": KERNEL_VERSION,
        "status": st, "status_line": status_line(st),
        "ui": {"tabs": list(TABS), "per_section": config.get("render.per_section", root)},
        "me": {
            "name": viewer, "current_task": tv, "approvals": av, "notices": nv,
            "learned": learned,
            "learned_summary": {"fixed": len(fixed), "fixed_items": fixed,
                                "prediction_hit": {"prev": prev["rate"], "now": this["rate"]}},
            "proposals": proposals_view(root),
        },
        "team": team,
        "pending_render": bool(todo_n or todo_a),
    }
    if write:
        write_json(paths.cache_dir(root) / "brief.json", b)
    return b


def load_cached(root, max_age_s=None):
    b = read_json(paths.cache_dir(root) / "brief.json", None)
    if not b:
        return None
    if max_age_s is not None:
        g = parse_iso(b.get("generated_at"))
        if not g or (now() - g).total_seconds() > max_age_s:
            return None
    return b


# ---------------- 대화창 쪽 경로(mod 없이도) ----------------

def inline_notices(root, area):
    """그 영역 작업 때 답변 안 한 줄씩 — 작업과 관계있는(영역이 겹치는) 꼭 알릴 일·흐름. 보인 것으로 표시한다.
    task 영역 report가 완료 보고 블록에 싣는다. 모델에게 주입하는 문구가 아니다."""
    if not root or not area:
        return []
    out, ids = [], []
    limit = config.get("render.per_section", root)
    for n in notices.pending(root, area):
        if n.get("level") not in LEVELS or not n.get("area") or not related(n["area"], area):
            continue
        text, _ = _render_text(root, n.get("text") or "", "notice", allow_llm=False)
        out.append("하네스: %s" % text)
        ids.append(n["id"])
        if len(out) >= limit:
            break
    mark_seen(root, ids)
    return out


def daily_two_lines(root, agent=None):
    """Codex·mod 없는 환경: 완료 보고 아래 하루 1회 2줄. 보일 것이 없으면 빈 목록(그날 몫을 쓰지 않는다)."""
    if not root:
        return []
    if agent == "claude":
        try:
            from vmc_harness.render import mod_install
            if mod_install.is_registered(root):
                return []
        except Exception:
            pass
    st = _state(root)
    today = now().strftime("%Y-%m-%d")
    if st.get("daily_lines_date") == today:
        return []
    b = build(root, write=False)
    unseen = [n for n in b["me"]["notices"] if not n["seen"]]
    n_app = len(b["me"]["approvals"])
    n_learn = len(b["me"]["learned"])
    if not (unseen or n_app or n_learn):
        return []
    lines = ["하네스 · 알림 %d · 확인 %d · 이번 주 배운 것 %d" % (len(unseen), n_app, n_learn)]
    must = [n for n in unseen if n["level"] == "must"]
    top = (must or unseen)
    if top:
        gid = (top[0].get("refs") or {}).get("guide_id")
        lines.append("%s (%s)" % (top[0]["text"], ("원복: vmc-harness finding --blame %s" % gid) if gid
                                  else "자세히: vmc-harness brief"))
        mark_seen(root, [top[0]["id"]])
    else:
        lines.append("자세히: vmc-harness brief")
    st = _state(root)
    st["daily_lines_date"] = today
    _save_state(root, st)
    return lines[:config.get("render.daily_lines", root)]


# ---------------- 글자 화면(`vmc-harness brief`) ----------------

def _sec(title):
    return "── %s %s" % (title, "─" * max(2, 34 - len(title) * 2))


def text(b, tab="me", all_items=False):
    per = b["ui"]["per_section"]
    lim = (lambda xs: xs) if all_items else (lambda xs: xs[:per])
    more = lambda xs: [] if all_items or len(xs) <= per else ["  … 더 보기 %d (vmc-harness brief --all)" % (len(xs) - per)]
    on = lambda name, key: "[%s]" % name if tab == key else name
    out = ["하네스   %s   %s" % (on("내 작업", "me"), on("팀 대시보드", "team")), ""]
    if tab == "team":
        t = b["team"]
        out[0] += "   %s" % t["week"]
        out.append(_sec("이번 주"))
        for r in t["numbers"]["rows"]:
            out.append("%-16s %s  %s" % (r["label"], r["value"], r.get("note") or ""))
        out += ["", _sec("확산"),
                "사용 인원          %d / %d  %s" % (t["spread"]["users"], t["spread"]["users_total"], " · ".join(t["spread"]["names"])),
                "공유된 판단        %d개  한 사람만 알던 규칙" % t["spread"]["shared_judgments"],
                "", _sec("하네스가 한 일"),
                "추가 %d · 원복 %d · 정리 %d" % (t["harness_did"]["added"], t["harness_did"]["reverted"], t["harness_did"]["cleaned"]),
                "", _sec("보고용 한 줄"), t["report_line"]]
    else:
        m = b["me"]
        out.append(_sec("지금 작업"))
        ct = m["current_task"]
        if ct:
            meta = " · ".join(x for x in [("예상 " + ct["expect"]) if ct["expect"] else None,
                                         ("%d분 경과" % ct["elapsed_min"]) if ct["elapsed_min"] is not None else None] if x)
            out.append("%s · %s" % (ct["title"], meta) if meta else ct["title"])
            if ct["evidence"]:
                for e in lim(ct["evidence"]):
                    out.append("증거 %s %s%s" % ("✓" if e["ok"] else "✗", e["label"], ("  " + e["file"]) if e.get("is_image") and e.get("file") else ""))
                out += more(ct["evidence"])
            if ct["out_of_scope"]:
                out.append("범위 밖 변경 %d건 · %s" % (len(ct["out_of_scope"]), ", ".join(x["path"] or "" for x in ct["out_of_scope"][:per])))
        else:
            out.append("(진행 중인 작업 없음)")
        out += ["", _sec("확인 필요")]
        for c in lim(m["approvals"]):
            out += c["lines"] + ["  승인 · 거절 · 자세히   (vmc-harness approval %s approve|reject|detail)" % c["approval_id"], ""]
        out += more(m["approvals"])
        if not m["approvals"]:
            out.append("(없음)")
        out += ["", _sec("알아둘 것")]
        for n in lim(m["notices"]):
            mark = {"must": "●", "flow": "○"}.get(n["level"], "·")
            out.append("%s %s · %s%s" % (mark, n["text"], n["when"], "  (본 알림)" if n["seen"] else ""))
            gid = (n.get("refs") or {}).get("guide_id")
            if gid:  # 원복 후보(selfcheck) — mod 없는 환경은 명령 한 줄로 같은 경로
                out.append("  원복: vmc-harness finding --blame %s   아님: vmc-harness notice dismiss %s" % (gid, n["id"]))
            else:
                out.append("  %s   (vmc-harness notice mute %s)" % (" · ".join(n["buttons"]), n["id"]))
        out += more(m["notices"])
        if not m["notices"]:
            out.append("(없음)")
        for p in lim(m.get("proposals") or []):
            out.append("◇ %s  추가 · 제외" % p["text"])
        out += ["", _sec("이번 주 배운 것")]
        for x in lim(m["learned"]):
            out.append("· %s%s  (%s)" % (x["text"], (" · " + x["author"]) if x.get("author") else "", x["source"]))
        out += more(m["learned"])
        ls = m["learned_summary"]
        ph = ls["prediction_hit"]
        tail = []
        if ls["fixed"]:
            tail.append("고친 판단 %d" % ls["fixed"])
        if ph["now"] is not None:
            tail.append("예측 적중 %s%d%%" % (("%d%% → " % round(ph["prev"] * 100)) if ph["prev"] is not None else "",
                                          round(ph["now"] * 100)))
        if tail:
            out.append(" · ".join(tail))
        elif not m["learned"]:
            out.append("(없음)")
    out += ["", b["status_line"]]
    return "\n".join(out)


# ---------------- 하루 1건 바깥 전달(`vmc-harness brief --top`) ----------------

def _reply(kind, item_id, refs):
    """답장 키워드 → 그대로 실행할 명령. 버튼과 같은 처리(spec 08 — 모든 버튼은 되돌릴 수 있다)."""
    if kind == "guide_conflict":
        return {"빼": "vmc-harness notice revert %s" % item_id, "유지": "vmc-harness notice dismiss %s" % item_id}
    if kind == "proposal_auto":
        return {"빼": "vmc-harness notice exclude %s" % item_id, "유지": None}
    if kind == "proposal":
        return {"넣어": "vmc-harness notice add %s" % item_id, "빼": "vmc-harness notice exclude %s" % item_id}
    if kind == "gate_disabled":
        return {"켜": "vmc-harness notice on %s" % item_id, "유지": "vmc-harness notice keep %s" % item_id}
    return {"그만": "vmc-harness notice mute %s" % item_id}


RAW_KINDS = ("guide_conflict",)


def _session_intent(root, session_id):
    """알림이 나온 대화의 착수 선언(하려는 것) — 하루 뒤 바깥에서 받아도 어느 작업 얘기인지 알게(2026-11-28). 없으면 None."""
    st = _task_state()
    if not session_id or st is None or not hasattr(st, "session_tasks"):
        return None
    try:
        tasks = st.session_tasks(root, session_id)
    except Exception:
        return None
    for t in reversed(tasks):
        d = t.get("declare")
        intent = d.get("intent") if isinstance(d, dict) else None
        if intent:
            intent = " ".join(str(intent).split())
            return intent if len(intent) <= 80 else intent[:79] + "…"
    return None


def _origin(item):
    """'11/27 21:44 · 작업 「…」' — 알림이 생긴 때와 작업. 둘 다 없으면 ''."""
    d = parse_iso(item.get("at")) if item.get("at") else None
    parts = []
    if d:
        d = d.astimezone()
        parts.append("%d/%d %02d:%02d" % (d.month, d.day, d.hour, d.minute))
    if item.get("task"):
        parts.append("작업 「%s」" % item["task"])
    return " · ".join(parts)


def top_item(root, mark=False):
    """사람이 답할 만한 가장 중요한 1건 — 매일 바깥 브리핑(디스코드 등)에 붙이는 용도(spec 08 §전달, 2026-11-18).
    순서: 정정과 부딪힌 지침(원복 후보) → 하네스 조치 원복·게이트 꺼짐·오류 → 자동 반영(시험 중) → 답 안 한 진단 제안 → 소식.
    mark=True면 고른 알림을 보인 것으로 표시(실제로 보낸 경우만). 진단 제안은 답할 때까지 다시 고른다.
    → {"kind","id","text","reply":{키워드: 명령|None},"more":남은 개수,"at":알림 시각|None,"task":그 대화의 작업|None} | None"""
    nv, _ = notices_view(root, None, allow_llm=False)
    order = list(config.get("render.top_kinds", root))
    unseen = [n for n in nv if not n["seen"] and n.get("kind") in order]
    props = proposals_view(root)
    auto = [p for p in props if p["buttons"] == ["제외"] and p["text"].startswith("자동 반영")]
    open_p = [p for p in props if p["buttons"] == ["추가", "제외"]]
    cand = []
    for n in unseen:
        # 원문 인용이 든 알림은 다듬지 않은 원문으로 — 다듬기가 인용을 바꿔 쓰면 무슨 말이었는지 못 알아본다(2026-11-28)
        text = n.get("raw") or n["text"] if n["kind"] in RAW_KINDS else n["text"]
        cand.append((order.index(n["kind"]), n.get("at") or "", {"kind": n["kind"], "id": n["id"], "text": text,
                                                                 "refs": n.get("refs") or {}, "_notice": True,
                                                                 "at": n.get("at")}))
    rank_auto = order.index("diagnosis_auto_applied") if "diagnosis_auto_applied" in order else len(order)
    for p in auto:
        cand.append((rank_auto, "", {"kind": "proposal_auto", "id": p["id"], "text": p["text"], "refs": {}}))
    rank_prop = order.index("diagnosis_weekly") if "diagnosis_weekly" in order else len(order)
    for p in open_p:
        cand.append((rank_prop - 0.5, "", {"kind": "proposal", "id": p["id"], "text": p["text"], "refs": {}}))
    if not cand:
        return None
    cand.sort(key=lambda c: (c[0], c[1]))
    best = cand[0][2]
    out = {"kind": best["kind"], "id": best["id"], "text": best["text"],
           "reply": _reply(best["kind"], best["id"], best["refs"]), "more": len(cand) - 1,
           "at": best.get("at"), "task": _session_intent(root, (best.get("refs") or {}).get("session_id"))}
    if mark and best.get("_notice"):
        mark_seen(root, [best["id"]])
    return out


def top_text(item):
    """사람 말 한 문단 + 답장 안내(명령은 받는 쪽 에이전트가 실행)."""
    if not item:
        return ""
    keys = [k for k, v in (item.get("reply") or {}).items() if v]
    tail = (" — 답장: %s" % " / ".join("'%s'" % k for k in keys)) if keys else ""
    more = (" (남은 소식 %d건: `vmc-harness brief`)" % item["more"]) if item.get("more") else ""
    origin = _origin(item)
    return "하네스%s: %s%s%s" % ((" (%s)" % origin) if origin else "", item["text"], tail, more)
