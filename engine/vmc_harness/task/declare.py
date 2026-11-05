"""착수 선언 (spec 03 §착수 선언, 09 작업 기록 declare 스키마).

본 에이전트가 첫 편집 전에 직접 쓴다. 사람은 보지 않는다 — 개발자에게는 "예상 8~15분" 한 줄만.
"""

import time

from vmc_harness.core import events, ids, inject
from vmc_harness.core.clock import now_iso
from vmc_harness.core.mask import mask
from vmc_harness.task import lifecycle, measure, scope, size as sizer, state as st

PCT = ("p30", "p50", "p80")


class DeclareError(ValueError):
    """빠진 것·어긋난 것만 담는다(01 P6)."""


def _list(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return [str(x).strip() for x in v if str(x).strip()]


def _triple(v, name, missing):
    if isinstance(v, (list, tuple)) and len(v) == len(PCT):
        v = dict(zip(PCT, v))
    if isinstance(v, str):
        parts = [x.strip() for x in v.split(",")]
        v = dict(zip(PCT, parts)) if len(parts) == len(PCT) else None
    if not isinstance(v, dict) or any(v.get(k) in (None, "") for k in PCT):
        missing.append("예측 %s P30/P50/P80" % name)
        return None
    try:
        out = {k: float(v[k]) for k in PCT}
    except (TypeError, ValueError):
        missing.append("예측 %s 숫자 아님" % name)
        return None
    if any(out[k] <= 0 for k in PCT):
        missing.append("예측 %s 0 이하" % name)
        return None
    if not (out["p30"] <= out["p50"] <= out["p80"]):
        missing.append("예측 %s 순서(P30≤P50≤P80) 어긋남" % name)
        return None
    return {k: (int(x) if float(x).is_integer() else x) for k, x in out.items()}


def normalize(raw):
    """입력(dict) → 09 스키마 declare. 빠진 게 있으면 DeclareError(빠진 항목만)."""
    raw = dict(raw or {})
    missing = []
    intent = mask(str(raw.get("intent") or "").strip())
    if not intent:
        missing.append("하려는 것(intent)")
    scope_l = [scope.norm(x) for x in _list(raw.get("scope"))]
    if not scope_l:
        missing.append("건드릴 범위(scope)")
    new_l = [scope.norm(x) for x in _list(raw.get("new"))]
    tc = raw.get("test_change")
    if tc in ("", False, None, {}):
        tc = None
    elif isinstance(tc, dict):
        files = [scope.norm(x) for x in _list(tc.get("files"))]
        reason = str(tc.get("reason") or "").strip()
        if not files or not reason:
            missing.append("테스트 수정의 파일·이유")
        tc = {"files": files, "reason": mask(reason)}
    else:
        missing.append("테스트 수정 형식({files, reason})")
        tc = None
    pred = raw.get("predict") or {}
    tokens = _triple(pred.get("tokens", raw.get("tokens")), "토큰", missing)
    sec = pred.get("agent_sec", raw.get("agent_sec"))
    if sec is None and (pred.get("agent_min") or raw.get("agent_min")):
        m = _triple(pred.get("agent_min") or raw.get("agent_min"), "에이전트 작업 시간(분)", missing)
        sec = {k: v * 60 for k, v in m.items()} if m else None
        agent_sec = _triple(sec, "에이전트 작업 시간", missing) if sec else None
    else:
        agent_sec = _triple(sec, "에이전트 작업 시간", missing)
    appr = raw.get("approvals_expected", 0)
    try:
        appr = int(appr or 0)
        if appr < 0:
            raise ValueError
    except (TypeError, ValueError):
        missing.append("승인 예상 건수")
        appr = 0
    if missing:
        raise DeclareError("착수 선언에 빠진 것: " + ", ".join(missing))
    out = {"intent": intent, "scope": scope_l, "new": new_l,
           "protected_touch": bool(raw.get("protected_touch")), "test_change": tc,
           "predict": {"tokens": tokens, "agent_sec": agent_sec}, "approvals_expected": appr}
    end = mask(str(raw.get("end") or "").strip())[:60]
    if end:
        out["end"] = end  # 어디까지 하나(문서만·커밋·배포) — 사람에게 보이는 착수 한 줄에 붙는다(spec 13)
    return out


def _entries(d):
    tc = d.get("test_change") or {}
    return [x for x in (d.get("scope") or []) + (d.get("new") or []) + (tc.get("files") or []) if x]


def _overlap(root, a, b):
    """두 선언의 범위가 겹치는가(한쪽 항목이 다른 쪽 범위 안)."""
    return any(scope.in_scope(root, a, e) for e in _entries(b)) or any(scope.in_scope(root, b, e) for e in _entries(a))


def same_task(root, prev, d, mode=None):
    """같은 세션의 진행 중 작업에 새 선언이 왔을 때 같은 작업의 갱신인가.
    mode: "same"(--same-task) | "new"(--new-task) | None(자동 — 아직 편집 전이거나 범위가 겹치면 같은 작업)."""
    if mode == "new":
        return False
    if mode == "same":
        return True
    if prev.get("ended_at") or lifecycle.is_idle(root, prev):
        return False
    return not prev.get("edits") or _overlap(root, prev.get("declare") or {}, d)


def merge_test_change(old, new):
    """같은 작업 재선언: 테스트 수정 허용 파일은 누적한다(현장 결함 C1). 이유는 다르면 이어 붙인다."""
    old = old or None
    if not old:
        return new
    if not new:
        return old
    files = list(old.get("files") or [])
    for f in new.get("files") or []:
        if f not in files:
            files.append(f)
    reasons = [r for r in (old.get("reason"), new.get("reason")) if r]
    reason = reasons[0] if len(set(reasons)) <= 1 else " / ".join(dict.fromkeys(reasons))
    return {"files": files, "reason": reason}


def declare(root, raw, session_id=None, agent=None, mode=None):
    """선언 저장. 같은 세션의 진행 중 작업과 같은 작업이면 갱신(테스트 수정 허용 누적), 아니면 그 작업을 마감하고 새 작업.
    {"task","size","created","lines":[출력 줄], "pm_mode", "closed"}"""
    t0 = time.time()
    d = normalize(raw)
    session_id = st.resolve_session_id(root, session_id)
    sess = st.session_get(root, session_id) or {}
    agent = agent or sess.get("agent") or "claude"
    prev = None
    if session_id:
        prev = st.current(root, session_id)
        if prev is None and mode == "same":
            mine = st.session_tasks(root, session_id)
            prev = mine[-1] if mine else None
    else:
        prev = st.current(root, None)
    closed = None
    task = None
    if prev is not None:
        if same_task(root, prev, d, mode):
            task = prev
            if task.get("ended_at"):
                task = lifecycle.reopen(root, task)
        else:
            closed = lifecycle.close(root, prev, "redeclare", refresh=True)
    created = task is None
    sz = sizer.judge(d, root)
    if created:
        inherit = []
        if closed is not None and closed.get("session_id") == session_id:
            inherit = list((closed.get("base") or {}).get("created") or []) + list(closed.get("created") or [])
        stamp = now_iso()
        task = {"task_id": ids.new_id("task"), "session_id": session_id, "agent": agent,
                "started_at": stamp, "declared_at": stamp, "declare_rev": 1,
                "declare": d, "size": sz, "base": scope.take_base(root, created=inherit),
                "first_edit_at": None, "last_edit_at": None, "ended_at": None, "edits": [], "created": [],
                "pm": {"called": False, "delivered": False, "mode": None},
                "cards_delivered": False,
                "overdesign": {"signals": [], "details": [], "reviewed": False, "lines_before": None,
                               "lines_after": None, "trimmed": None, "why_not_smaller": None,
                               "llm_spawned": False},
                "out_of_scope_reasons": {}, "overhead": {"sec": 0.0, "tokens": 0}, "actions": []}
        changed = True
        lifecycle.start(root, task)
        try:
            lifecycle.sweep(root)   # 같은 세션에 남은 조용한 작업·다른 세션의 조용한 작업 마감(가벼움)
        except Exception:
            pass
    else:
        d["test_change"] = merge_test_change(task["declare"].get("test_change"), d.get("test_change"))
        sz = sizer.judge(d, root)
        changed = task.get("declare") != d
        task["declare"] = d
        task["size"] = sz
        task["last_activity_at"] = now_iso()
        if changed:
            task["declare_rev"] = int(task.get("declare_rev") or 1) + 1
            task["redeclared_at"] = now_iso()
    st.save(root, task)
    lines = []
    est = measure.estimate_line(d)
    if est:
        lines.append(est)
    ctx_parts = []
    sim = measure.similar_text(measure.similar(root, d))
    if sim:
        ctx_parts.append(sim)
    pm_mode = None
    from vmc_harness.core import budget
    if sz == sizer.MID and changed and (budget.enabled(root, "pm") or budget.enabled(root, "card_search")):
        from vmc_harness.task import pm
        pm_mode = pm.start(root, task, agent)
        task = st.by_id(root, task["task_id"])
        task["pm"]["called"] = True
        task["pm"]["mode"] = pm_mode
        if pm_mode == "sync":
            rec = pm.read_bg(root, task["task_id"]) or {}
            ct = pm.cards_text(rec.get("cards"))
            if ct:
                ctx_parts.append(ct)
                task["cards_delivered"] = True
        st.save(root, task)
    if ctx_parts:
        msg = inject.message("declare_context", "\n".join(ctx_parts))
        if msg:
            lines.append(msg)
    lines.append("작업 %s · 크기 %s%s" % (task["task_id"], sz, "" if created else " · 선언 갱신(같은 작업)"))
    if closed is not None:
        lines.append("이전 작업 %s 마감 — 같은 작업을 고쳐 선언한 것이면 --same-task" % closed["task_id"])
    task = st.by_id(root, task["task_id"]) or task
    o = task.setdefault("overhead", {"sec": 0.0, "tokens": 0})
    o["declare_sec"] = round(float(o.get("declare_sec") or 0) + (time.time() - t0), 3)
    st.save(root, task)
    try:
        events.log(root, "declare", task_id=task["task_id"], session_id=session_id, size=sz,
                   created=created, pm_mode=pm_mode, closed=(closed or {}).get("task_id"))
    except Exception:
        pass
    lifecycle.publish(root, task, "open")
    if closed is not None:
        lifecycle.spawn_measure(root, closed["task_id"])
    return {"task": task, "size": sz, "created": created, "lines": lines, "pm_mode": pm_mode, "closed": closed}


def why_not_smaller(root, reason, session_id=None):
    """과설계 축소 검토에 이유 한 줄(장부 overdesign.why_not_smaller)."""
    reason = mask(str(reason or "").strip())
    if not reason:
        raise DeclareError("이유 없음")
    session_id = st.resolve_session_id(root, session_id)
    task = st.current(root, session_id)
    if not task:
        raise DeclareError("착수 선언 없음")
    task.setdefault("overdesign", {})["why_not_smaller"] = reason
    st.save(root, task)
    return task
