"""task 영역 훅 핸들러 (hooks/contract.py). 편의 장치 — 예외는 디스패처가 통과 처리(fail-open).
단 "착수 선언 없음"은 막는다(소 작업 예외 없음 — 선언은 항상).

- on_session_start / on_user_prompt : 세션 상태 기록 · 조용한 작업 마감 표시(가벼움 — 측정은 백그라운드)
- on_pre_tool  : 쓰기 첫 편집에 선언 확인 · 편집 경로 기록 · 선언 범위 밖 첫 편집 직전에만 PM 결과를 기다려
                 pm_opinion 1회 · (첫 편집 전 읽기 도구에서) 착수 카드 1회
- on_post_tool : 착수 카드가 늦게 준비됐으면 첫 편집 전까지 1회
- on_stop      : 과설계 점검(축소 검토 1회만) · 요청 밖 변경 집계 · 완료 보고 블록 준비 · 장부 "진행 중" 갱신
- on_session_end : 마감 표시만 + 측정은 백그라운드(task.lifecycle)

작업 중 숫자(예측·예산·실측)는 주입하지 않는다(01 P5) — 문구는 core.inject.message()로만.
"""

import time
from pathlib import Path

from vmc_harness.core import config, events, inject
from vmc_harness.core.clock import now_iso
from vmc_harness.hooks.contract import HookResult
from vmc_harness.task import state as st

WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch", "str_replace_editor",
               "create_file", "edit_file")
NO_DECLARE = "착수 선언 없음"
# 빠진 것과 그걸 채우는 도구·항목(01 P6 — 절차가 아니라 빠진 것과 도구). 실측: 차단 뒤 에이전트가 매번 --help로 한 번 더 왕복(2026-11-11)
# 예측 항목 이름은 싣지 않는다 — 작업 중 주입에 예측·토큰 낱말 금지(01 P5, 빠지면 declare가 빠진 항목을 알려 준다)
NO_DECLARE_DETAIL = "%s — 도구 `vmc-harness declare`, 필요한 항목: 하려는 것(--intent)·건드릴 범위(--scope)·예측" % NO_DECLARE
OVERDESIGN_GATE = "overdesign"


def _inside(p):
    s = str(p or "").replace("\\", "/")
    return bool(s) and not s.startswith(("/", "../")) and ":" not in s[:3]


def _write_paths(ctx):
    paths = [p for p in (ctx.write_paths or []) if _inside(p)]
    # 쓰기 도구인데 경로를 못 뽑았을 때만 "쓰기"로 본다. 어댑터가 경로를 뽑았는데 전부 프로젝트 밖이면 쓰기 아님
    unknown = ctx.tool_name in WRITE_TOOLS and not ctx.write_paths and not (ctx.state or {}).get("file_ops")
    is_write = bool(paths) or unknown
    return is_write, paths


def _bind(ctx, task):
    if task:
        ctx.task_id = ctx.task_id or task["task_id"]
        ctx.state["task"] = task
    return task


def _done(root, task, t0):
    o = task.setdefault("overhead", {"sec": 0.0, "tokens": 0})
    o["sec"] = round(float(o.get("sec") or 0) + (time.time() - t0), 3)
    st.save(root, task)


def _cards_inject(root, task):
    """착수 카드(중·대, 비동기 결과가 준비됐을 때) — 착수 시점 정보라 declare_context."""
    if task.get("cards_delivered") or task.get("size") != "mid":
        return None
    from vmc_harness.task import pm
    rec = pm.read_bg(root, task["task_id"])
    if not rec or rec.get("status") != "done":
        return None
    task["cards_delivered"] = True
    return inject.message("declare_context", pm.cards_text(rec.get("cards"))) if rec.get("cards") else None


def on_session_start(ctx):
    if not ctx.root or not ctx.session_id:
        return None
    root = ctx.root
    st.session_touch(root, ctx.session_id, ctx.agent, ctx.transcript_path, ctx.cwd, start=True)
    try:
        st.prune_sessions(root, config.get("task.session_keep_days", root))
    except Exception:
        pass
    # 하루 1회: 가벼움 예산 판정(넘으면 편의 장치 하나 끔 — 01 P8) · 로컬 원본 보관 기간 정리(09 retention.local_days)
    from vmc_harness.core import budget, retention
    for fn in (budget.check, retention.prune):
        try:
            fn(root)
        except Exception:
            pass
    # 종료 감지에 기대지 않는다(task.lifecycle): 다른 세션의 조용한 작업은 마감 표시만, 무거운 측정은 백그라운드
    from vmc_harness.task import lifecycle
    try:
        lifecycle.sweep(root, exclude_session=ctx.session_id)
        if lifecycle.pending(root):
            lifecycle.spawn_measure(root)
    except Exception:
        pass
    # resume으로 세션 id가 바뀌었으면 앞 세션의 작업을 이 세션으로 잇는다(spec 13)
    try:
        lifecycle.adopt_resumed(root, ctx.session_id, ctx.transcript_path)
    except Exception:
        pass
    # resume: 이 세션의 작업이 이어지면 그 사이 바깥(사람) 변경을 스냅샷에 넣는다
    mine = st.current(root, ctx.session_id)
    if mine and mine.get("session_id") == ctx.session_id:
        try:
            from vmc_harness.task import scope
            scope.absorb_outside_changes(root, mine)
            st.save(root, mine)
        except Exception:
            pass
    _bind(ctx, st.current(root, ctx.session_id))
    return None


def on_user_prompt(ctx):
    if not ctx.root or not ctx.session_id:
        return None
    st.session_touch(ctx.root, ctx.session_id, ctx.agent, ctx.transcript_path, ctx.cwd, prompt=True)
    _bind(ctx, st.current(ctx.root, ctx.session_id))
    return None


def on_pre_tool(ctx):
    if not ctx.root:
        return None
    t0 = time.time()
    root = ctx.root
    if ctx.session_id:
        st.session_touch(root, ctx.session_id, ctx.agent, ctx.transcript_path, ctx.cwd)
    task = _bind(ctx, st.current(root, ctx.session_id, reopen=True))
    is_write, paths = _write_paths(ctx)
    if not is_write:
        if task and not task.get("first_edit_at"):
            msg = _cards_inject(root, task)
            if msg:
                _done(root, task, t0)
                return HookResult(inject=[msg])
        return None
    if not task:
        try:
            events.log(root, "gate_block", gate_id="kernel.declare", tool=ctx.tool_name)
        except Exception:
            pass
        return HookResult(block=True, reason=inject.message("gate_violation", NO_DECLARE_DETAIL))
    from vmc_harness.task import scope
    injects = []
    stamp = now_iso()
    task["first_edit_at"] = task.get("first_edit_at") or stamp
    task["last_edit_at"] = stamp
    task["last_activity_at"] = stamp
    edits = task.setdefault("edits", [])
    created = task.setdefault("created", [])
    for p in paths:
        p = scope.norm(p)
        if p not in edits:
            edits.append(p)
        if p not in created and not (Path(root) / p).exists():
            created.append(p)   # 이 작업이 새로 만든 파일 — 커밋한 뒤에도 "기존 검증물"이 아니다(현장 결함 C2)
    # 착수 카드는 첫 편집 전(읽기 도구·PostToolUse)에만 — 디스패처가 작업 중 declare_context를 버린다
    outside = [p for p in paths if not scope.in_scope(root, task.get("declare") or {}, p)
               and not scope.norm(p).startswith(scope.HARNESS_PREFIX)]
    pmst = task.setdefault("pm", {})
    if outside and task.get("size") == "mid" and pmst.get("called") and not pmst.get("delivered"):
        pmst["delivered"] = True
        pmst["delivered_at"] = stamp
        from vmc_harness.task import pm
        rec = pm.wait(root, task["task_id"], config.get("pm.wait_s", root))
        text = pm.opinion_text(rec)
        m = inject.message("pm_opinion", text) if text else None
        if m:
            injects.append(m)
        try:
            events.log(root, "pm_wait", task_id=task["task_id"], ready=bool(rec), injected=bool(m))
        except Exception:
            pass
    _done(root, task, t0)
    return HookResult(inject=injects) if injects else None


def on_post_tool(ctx):
    if not ctx.root or not ctx.session_id:
        return None
    t0 = time.time()
    task = _bind(ctx, st.current(ctx.root, ctx.session_id))
    if not task or task.get("first_edit_at"):
        return None
    msg = _cards_inject(ctx.root, task)
    if not msg:
        return None
    _done(ctx.root, task, t0)
    return HookResult(inject=[msg])


def on_stop(ctx):
    if not ctx.root or not ctx.session_id:
        return None
    t0 = time.time()
    root = ctx.root
    task = _bind(ctx, st.current(root, ctx.session_id))
    if not task:
        return None
    from vmc_harness.task import lifecycle, overdesign, report, scope
    files = scope.touched(root, task)
    if not files:
        _done(root, task, t0)
        lifecycle.touch(root, st.by_id(root, task["task_id"]) or task, files=[], out_of_scope=[],
                        touch_ratio=scope.touch_ratio(root, task, []))
        return None
    od = task.setdefault("overdesign", {})
    result = None
    if od.get("reviewed"):
        # 축소 검토는 1회만 — 이후엔 줄었는지만 잰다
        od["lines_after"] = scope.added_lines(root, task, files)
        if od.get("lines_before") is not None:
            od["trimmed"] = od["lines_after"] < od["lines_before"]
    else:
        signals = overdesign.machine_signals(root, task, files)
        from vmc_harness.core import budget
        if task.get("size") == "mid" and budget.enabled(root, "overdesign_llm"):
            if not od.get("llm_spawned"):
                od["llm_spawned"] = True
                st.save(root, task)
                overdesign.spawn_llm(root, task["task_id"], ctx.transcript_path, ctx.agent)
                task = st.by_id(root, task["task_id"]) or task
                od = task.setdefault("overdesign", od)
            llm = overdesign.read_llm(root, task["task_id"], files)
            if llm:
                signals += llm.get("signals") or []
        if signals and _proceeded(ctx):
            od["reviewed"] = True   # 사람이 "그냥 진행"으로 넘긴 과설계 차단 — 다시 막지 않는다
            od["proceeded"] = True
            signals = []
        if signals:
            od["reviewed"] = True
            od["reviewed_at"] = now_iso()
            od["signals"] = sorted({s["kind"] for s in signals})
            od["details"] = signals
            od["lines_before"] = scope.added_lines(root, task, files)
            reason = inject.message("gate_violation", overdesign.describe(signals)) or \
                inject.message("gate_violation", overdesign.describe([{"kind": k} for k in od["signals"]]))
            try:
                events.log(root, "gate_block", gate_id="kernel.overdesign", task_id=task["task_id"],
                           signals=od["signals"])
            except Exception:
                pass
            try:
                from vmc_harness.ledger.exposure import record
                record(root, gate_id="kernel.overdesign")
            except Exception:
                pass
            result = HookResult(block=True, reason=reason)
            _record_block(ctx, reason)
    st.save(root, task)
    rep = report.build(root, task)
    task = st.by_id(root, task["task_id"]) or task
    lifecycle.touch(root, task, files=files, out_of_scope=rep.get("out_of_scope"),
                    touch_ratio=scope.touch_ratio(root, task, files))
    if result is None:
        lines = [rep["text"].splitlines()[0]] if rep["out_of_scope"] else []
        lines += report.notice_lines(root, rep, ctx.agent)
        if lines:
            result = HookResult(notice="\n".join(lines))
    _done(root, task, t0)
    return result


def _record_block(ctx, reason):
    """과설계 차단도 게이트 차단 기록에 남긴다 — `proceed`가 가장 최근 차단으로 고를 수 있게(현장 결함 C7)."""
    try:
        from vmc_harness.gates import state as gstate
        gstate.record_block(ctx.root, ctx, OVERDESIGN_GATE, reason, gate_id="kernel:%s" % OVERDESIGN_GATE)
    except Exception:
        pass


def _proceeded(ctx):
    try:
        from vmc_harness.gates import state as gstate
        return gstate.consume_bypass(ctx.root, ctx, OVERDESIGN_GATE)
    except Exception:
        return False


def on_session_end(ctx):
    """마감 표시만(가벼움). 세션 기록 파싱·git 측정은 백그라운드 — 다시 열리면(resume) 이어 붙인다."""
    if not ctx.root or not ctx.session_id:
        return None
    root = ctx.root
    st.session_touch(root, ctx.session_id, ctx.agent, ctx.transcript_path, ctx.cwd)
    task = st.current(root, ctx.session_id)
    if not task:
        return None
    from vmc_harness.task import lifecycle
    lifecycle.close(root, task, "session_end")
    lifecycle.spawn_measure(root, task["task_id"])
    return None
