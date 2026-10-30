"""완료 보고 맨 위 블록 (spec 03 §완료 보고).

순서: 범위 밖 변경 → 영향(모듈) → 증거 → 승인 카드. 에이전트가 `vmc-harness report`로 받아 완료 보고 맨 위에 붙인다.
렌더링 엔진(08)이 있으면 그 사람 말로 다시 쓰고, 기계 검사(사실 보존)를 통과한 경우만 쓴다.
"""

from vmc_harness.core import config
from vmc_harness.core.jsonio import write_json
from vmc_harness.task import scope, state as st


def _modules(root, files):
    try:
        from vmc_harness.bootstrap.structmap import module_of
    except ImportError:
        module_of = None
    mods = []
    for f in files:
        m = None
        if module_of:
            try:
                m = module_of(root, f)
            except Exception:
                m = None
        m = m or (f.rsplit("/", 1)[0] if "/" in f else ".")
        if m not in mods:
            mods.append(m)
    return mods


def _evidence(root, task_id):
    try:
        from vmc_harness.gates.evidence import list_markers
        return list_markers(root, task_id) or []
    except Exception:
        return []


def _approvals(root, task_id):
    try:
        from vmc_harness.core import ledger
        return [a for a in ledger.list_approvals(root) if a and a.get("task_id") == task_id and not a.get("answer")]
    except Exception:
        return []


def build(root, task, allow_llm=False):
    """allow_llm=False(훅 — 시간 상한 안): 렌더링 검사만. `vmc-harness report`는 True(검사 실패 때만 1회 재작성)."""
    files = scope.touched(root, task)
    oos = scope.out_of_scope(root, task, files)
    mods = _modules(root, files)
    ev = _evidence(root, task["task_id"])
    appr = _approvals(root, task["task_id"])
    cap = config.get("task.report_max_lines", root)
    lines = ["범위 밖 변경 %d건" % len(oos)]
    for o in oos[:cap]:
        why = o.get("reason") or "+%d −%d줄" % (o.get("added") or 0, o.get("removed") or 0)
        lines.append("- %s: %s" % (o["path"], why))
    if len(oos) > cap:
        lines.append("- 외 %d건" % (len(oos) - cap))
    if mods:
        lines.append("영향: %s" % ", ".join(mods[:cap]) + (" 외 %d개" % (len(mods) - cap) if len(mods) > cap else ""))
    if ev:
        kinds = {}
        for m in ev:
            kinds[m.get("kind")] = kinds.get(m.get("kind"), 0) + 1
        lines.append("증거: %s" % " · ".join("%s %d" % (k, n) for k, n in sorted(kinds.items())))
    else:
        lines.append("증거: 없음")
    if appr:
        lines.append("확인 필요: 승인 %d건 (%s)" % (len(appr), ", ".join(a["approval_id"] for a in appr)))
    text = "\n".join(lines)
    facts = {"out_of_scope": [{"path": o["path"], "reason": o.get("reason")} for o in oos],
             "modules": mods, "evidence": [m.get("kind") for m in ev],
             "approvals": [a["approval_id"] for a in appr]}
    text = _render(root, facts, text, allow_llm)
    rec = {"task_id": task["task_id"], "text": text, "out_of_scope": oos, "modules": mods,
           "evidence": ev, "approvals": [a["approval_id"] for a in appr], "files": files}
    write_json(st.task_path(root, task["task_id"], ".report"), rec)
    return rec


def _render(root, facts, draft, allow_llm):
    try:
        from vmc_harness.render.engine import render
    except ImportError:
        return draft
    try:
        res = render(root, None, facts, draft=draft, kind="report", allow_llm=allow_llm, cache=True) or {}
    except Exception:
        return draft
    if res.get("rewritten") and (res.get("checks") or {}).get("facts_ok") and res.get("text"):
        return res["text"]
    return draft


def notice_lines(root, rec, agent=None):
    """완료 보고 아래 사람에게만 보일 줄(spec 03·08) — 그 작업 영역의 꼭 알릴 일 한 줄씩(render.brief.inline_notices)
    + Codex·mod 없는 환경의 하루 1회 2줄(daily_two_lines). 둘 다 보인 것으로 표시되므로 실제로 보일 때만 부른다
    (Stop 훅이 막지 않고 끝날 때). 모델에게 주입하지 않는다(01 P5). render 영역이 없으면 빈 목록."""
    try:
        from vmc_harness.render import brief
    except ImportError:
        return []
    lines = []
    areas = []
    for a in list(rec.get("modules") or []) + [f.rsplit("/", 1)[0] for f in rec.get("files") or [] if "/" in f]:
        if a and a != "." and a not in areas:
            areas.append(a)
    for a in areas:
        try:
            for ln in brief.inline_notices(root, a) or []:
                if ln not in lines:
                    lines.append(ln)
        except Exception:
            pass
    try:
        lines.extend(brief.daily_two_lines(root, agent) or [])
    except Exception:
        pass
    return lines


def record_reason(root, task, path, reason):
    """범위를 넓힌 이유 한 줄(선택 — 강제하지 않는다)."""
    task.setdefault("out_of_scope_reasons", {})[scope.norm(path)] = str(reason or "").strip()[:200]
    st.save(root, task)
    return task
