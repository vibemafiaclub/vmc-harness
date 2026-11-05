"""작업 수명주기 — 언제 시작하고 언제 끝났나 (현장 결함 원인 ①, spec 03·09).

종료를 감지하려 하지 않는다.
- 착수 선언 때 팀 장부에 "진행 중"(status=open)으로 먼저 쓰고, 매 턴(Stop) 가볍게 덮어쓴다(파일 하나)
- "끝남"은 셋 중 하나: 같은 세션의 새 작업 선언(redeclare) · SessionEnd(session_end) ·
  마지막 활동 뒤 task.finalize_idle_min 경과(idle — 끝난 시각 = 마지막 활동 시각)
- idle 판정은 장부·로컬 상태를 읽는 쪽 어디서든 같은 함수(is_idle·effective)로 계산한다 —
  다른 세션 SessionStart, 같은 세션 resume, 세션 없는 작업, 밤 루프, brief
- 마감은 표시만(가볍게). 무거운 측정(세션 기록 파싱·git·이벤트 합산)은 measure_pending — 백그라운드(task-bg --measure)나 밤 루프
- 마감된 뒤 같은 세션에서 도구 활동이 오면 다시 열어(구간 segments 추가) 이어 붙인다
"""

import sys

from vmc_harness.core import config
from vmc_harness.core.clock import now, now_iso, parse_iso
from vmc_harness.task import state as st

REOPENABLE = ("idle", "session_end")   # 같은 세션 활동이 오면 다시 여는 마감 이유(새 선언으로 끝난 작업은 안 연다)


# ---- 판정(부작용 없음) ----

def _idle_min(root):
    return float(config.get("task.finalize_idle_min", root))


def last_activity(task):
    """마지막 활동 시각(aware datetime). 배경 기록(실측·PM 결과)은 활동이 아니다 — updated_at을 쓰지 않는다."""
    xs = [parse_iso(task.get(k)) for k in ("last_activity_at", "last_edit_at", "redeclared_at", "declared_at",
                                           "started_at")]
    xs = [x for x in xs if x]
    return max(xs) if xs else None


def is_idle(root, task, at=None):
    """끝나지 않은 작업이 마지막 활동 뒤 task.finalize_idle_min 넘게 조용한가."""
    if task.get("ended_at"):
        return False
    last = last_activity(task)
    if last is None:
        return False
    return ((at or now()) - last).total_seconds() / 60.0 >= _idle_min(root)


def effective(root, rec, at=None):
    """장부 기록 한 줄 → 지금 기준 상태. 진행 중인데 조용한 지 오래면 마지막 활동 시각에 끝난 것으로(쓰지 않는다)."""
    if rec.get("status") != "open":
        return rec
    last = parse_iso(rec.get("last_activity_at") or rec.get("started_at"))
    if last is None:
        return rec
    if ((at or now()) - last).total_seconds() / 60.0 < _idle_min(root):
        return rec
    return dict(rec, status="ended", ended_at=rec.get("last_activity_at") or rec.get("started_at"),
                end_reason="idle")


# ---- 장부 기록(가벼움 — git·세션 기록을 읽지 않는다) ----

def _windows(task):
    return task.get("segments") or [{"start": task.get("declared_at") or task.get("started_at"),
                                      "end": task.get("ended_at")}]


def ledger_record(root, task, status):
    """로컬 작업 상태 → 팀 장부 한 건(09 스키마 + status·end_reason·last_activity_at·segments)."""
    from vmc_harness.task import pm
    bg = pm.read_bg(root, task["task_id"]) or {}
    pmr = bg.get("pm") or {}
    cache = task.get("scope_cache") or {}
    od = task.get("overdesign") or {}
    over = task.get("overhead") or {}
    sec = task.get("measured_overhead_sec")
    if sec is None:
        sec = float(over.get("sec") or 0) + float(over.get("declare_sec") or 0)
    actual = task.get("actual") or {"tokens": None, "agent_sec": None, "tool_calls": None, "human_turns": None,
                                     "measured": False}
    return {
        "task_id": task["task_id"], "session_id": task.get("session_id"), "agent": task.get("agent") or "claude",
        "status": status, "started_at": task.get("started_at"), "ended_at": task.get("ended_at"),
        "end_reason": task.get("end_reason"), "last_activity_at": (last_activity(task) or now()).isoformat(
            timespec="seconds"),
        "segments": _windows(task),
        "declare": task.get("declare"), "size": task.get("size"),
        "pm": {"called": bool(pmr.get("called")), "opinion": pmr.get("opinion"), "wish": pmr.get("wish")},
        "actual": actual,
        "overdesign": {"signals": od.get("signals") or [], "trimmed": od.get("trimmed"),
                       "why_not_smaller": od.get("why_not_smaller")},
        "out_of_scope": [{"path": o["path"], "reason": o.get("reason")} for o in cache.get("out_of_scope") or []],
        "evidence": task.get("evidence_list") or [],
        "harness_overhead": {"sec": round(float(sec), 3), "tokens": int(over.get("tokens") or 0),
                             "gate_sec": task.get("gate_sec")},
        "touch": cache.get("touch") or {"in_scope": None, "declared_used": None},
        "measured_at": task.get("measured_at"),
        "session_ids": task.get("session_ids") or ([task["session_id"]] if task.get("session_id") else []),
        "phases": task.get("phases"), "phases_detail": task.get("phases_detail"), "analysis": task.get("analysis"),
    }


def publish(root, task, status=None):
    from vmc_harness.core import ledger
    status = status or ("ended" if task.get("ended_at") else "open")
    try:
        return ledger.write_task(root, ledger_record(root, task, status))
    except Exception:
        return None


def start(root, task):
    """착수 선언 직후(새 작업): 구간 하나를 열고 장부에 "진행 중"으로."""
    at = task.get("declared_at") or now_iso()
    task["last_activity_at"] = at
    task["segments"] = [{"start": at, "end": None}]
    st.save(root, task)
    publish(root, task, "open")
    return task


def touch(root, task, files=None, out_of_scope=None, touch_ratio=None):
    """매 턴(Stop): 마지막 활동 시각 + 이번 턴에 이미 계산한 범위 결과를 로컬 상태와 장부에 덮어쓴다(추가 git 호출 없음)."""
    task["last_activity_at"] = now_iso()
    if files is not None:
        task["scope_cache"] = {"at": task["last_activity_at"], "files": list(files),
                               "out_of_scope": [{"path": o["path"], "reason": o.get("reason")}
                                                for o in out_of_scope or []],
                               "touch": touch_ratio or {"in_scope": None, "declared_used": None}}
    st.save(root, task)
    publish(root, task, "open")
    return task


def refresh_scope(root, task):
    """범위 결과를 지금 작업 트리로 다시 계산(git) — 마감 직전 Stop 뒤 편집이 있었을 때만."""
    from vmc_harness.task import scope
    files = scope.touched(root, task)
    oos = scope.out_of_scope(root, task, files)
    task["scope_cache"] = {"at": now_iso(), "files": files,
                           "out_of_scope": [{"path": o["path"], "reason": o.get("reason")} for o in oos],
                           "touch": scope.touch_ratio(root, task, files)}
    return task


def _scope_stale(task):
    cache = task.get("scope_cache") or {}
    if not cache:
        return bool(task.get("edits"))
    edit = parse_iso(task.get("last_edit_at"))
    at = parse_iso(cache.get("at"))
    return bool(edit and at and edit > at)


def close(root, task, reason, at=None, refresh=False):
    """마감 표시(가벼움). refresh=True면 범위만 지금 다시 잰다(새 선언 — CLI라 훅 시간 상한 밖)."""
    if task.get("ended_at"):
        return task
    end = at or now_iso()
    if refresh and _scope_stale(task):
        try:
            refresh_scope(root, task)
        except Exception:
            pass
    task["ended_at"] = end
    task["end_reason"] = reason
    segs = _windows(task)
    if segs and not segs[-1].get("end"):
        segs[-1]["end"] = end
    task["segments"] = segs
    task["measure_pending"] = True
    st.save(root, task)
    publish(root, task, "ended")
    return task


def reopen(root, task):
    """마감된(또는 조용해진) 작업에 같은 세션 활동이 왔다 — 새 구간을 열고 이어 붙인다."""
    segs = _windows(task)
    last = last_activity(task)
    if segs and not segs[-1].get("end"):
        segs[-1]["end"] = task.get("ended_at") or (last.isoformat(timespec="seconds") if last else now_iso())
    stamp = now_iso()
    segs.append({"start": stamp, "end": None})
    task["segments"] = segs
    task["ended_at"] = None
    task["end_reason"] = None
    task["reopened"] = int(task.get("reopened") or 0) + 1
    task["last_activity_at"] = stamp
    task["measure_pending"] = False
    try:
        from vmc_harness.task import scope
        scope.absorb_outside_changes(root, task)
    except Exception:
        pass
    st.save(root, task)
    publish(root, task, "open")
    return task


def reopen_for_session(root, session_id):
    """이 세션의 마지막 작업이 조용해서·세션 종료로 마감됐으면 다시 연다. 없으면 None."""
    mine = st.session_tasks(root, session_id)
    if not mine:
        return None
    mine.sort(key=lambda t: (last_activity(t) or now()).isoformat())
    t = mine[-1]
    if t.get("ended_at") and t.get("end_reason") not in REOPENABLE:
        return None
    if not t.get("ended_at") and not is_idle(root, t):
        return t
    return reopen(root, t)


def sweep(root, exclude_session=None, at=None):
    """조용한 작업 마감·같은 세션의 겹친 진행 중 작업 정리(가벼움 — git·세션 기록 안 읽음). 마감한 작업 목록."""
    closed = []
    opened = st.open_tasks(root, include_idle=True)
    latest = {}
    for t in opened:
        sid = t.get("session_id")
        if sid:
            cur = latest.get(sid)
            if cur is None or (t.get("started_at") or "") >= (cur.get("started_at") or ""):
                latest[sid] = t
    for t in opened:
        sid = t.get("session_id")
        last = last_activity(t)
        last_iso = last.isoformat(timespec="seconds") if last else None
        if sid and latest.get(sid) is not t:
            closed.append(close(root, t, "superseded", at=last_iso))
            continue
        if sid and sid == exclude_session:
            continue
        if is_idle(root, t, at=at):
            closed.append(close(root, t, "idle", at=last_iso))
    return closed


# ---- 무거운 측정(백그라운드·밤 루프) ----

def pending(root):
    return [t for t in st.all_tasks(root) if t.get("ended_at") and t.get("measure_pending")]


def measure_one(root, task):
    from vmc_harness.task import measure
    if _scope_stale(task):
        try:
            refresh_scope(root, task)
        except Exception:
            pass
    rec = measure.finalize(root, task)
    return rec


def measure_pending(root, limit=None):
    """마감됐지만 아직 안 잰 작업을 잰다. 잰 작업 기록 목록."""
    out = []
    for t in pending(root)[:limit] if limit else pending(root):
        try:
            r = measure_one(root, t)
            if r:
                out.append(r)
        except Exception:
            continue
    return out


def spawn_measure(root, task_id=None):
    """측정을 백그라운드로(훅이 기다리지 않게). task_id 없으면 밀린 것 전부."""
    try:
        from vmc_harness.core.proc import spawn_detached
        from vmc_harness.task import pm
        spawn_detached([sys.executable, "-m", "vmc_harness", "task-bg", task_id or "-", "--measure",
                        "--root", str(root)], cwd=str(root), env=pm._env())
        return True
    except Exception:
        return False


def adopt_resumed(root, session_id, transcript_path):
    """이어받기(resume)로 세션 id가 바뀌었으면 앞 세션의 마지막 작업을 이 세션으로 옮긴다(spec 13).
    새 기록 파일은 앞 기록을 같은 uuid로 복사해 담으므로(2026-11-05 실측) 첫 메시지 uuid가 같은 앞 세션을 찾는다.
    측정은 옮긴 뒤의 세션 파일(마지막 것) 하나만 — 두 파일을 더하면 겹친 구간이 두 번 세진다. 옮긴 작업 또는 None."""
    from vmc_harness import sessions as _s
    if not session_id or not transcript_path or st.session_tasks(root, session_id):
        return None
    fu = _s.first_uuid(transcript_path)
    if not fu:
        return None
    for rec in st.sessions(root):
        old = rec.get("session_id")
        tp = rec.get("transcript_path")
        if not old or old == session_id or not tp or tp == transcript_path:
            continue
        if _s.first_uuid(tp) != fu:
            continue
        mine = [t for t in st.session_tasks(root, old) if t.get("end_reason") not in ("redeclare", "superseded")]
        if not mine:
            return None
        t = mine[-1]
        ids = list(t.get("session_ids") or [old])
        if session_id not in ids:
            ids.append(session_id)
        t["session_ids"] = ids
        t["session_id"] = session_id
        st.save(root, t)
        return t
    return None


def nightly(root):
    """밤 루프가 부른다: 마감 판정 + 밀린 측정. 실패해도 루프를 멈추지 않는다."""
    try:
        sweep(root)
        return measure_pending(root)
    except Exception:
        return []
