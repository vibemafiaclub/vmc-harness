"""훅 디스패처 (L0 — spec 02 §훅 디스패처). 에이전트가 부르는 진입점은 `vmc-harness hook <event> --agent <claude|codex>` 하나.

stdin JSON → 어댑터 정규화(HookContext) → HANDLER_MODULES 순서 호출 → 에이전트별 출력.
- VMC_HARNESS_NESTED=1(하네스가 띄운 claude -p 등)이면 즉시 통과. 하네스 미설치 프로젝트면 통과
- 안전 모듈(safety=True) 예외 → 막기(fail-closed, PreToolUse·Stop). 그 외 예외 → 통과 + events(gate_error)
- block이 나오면 그 자리에서 멈춘다(안전 게이트가 앞)
- 주입은 core.inject 허용 목록만(01 P5): 유형 태그를 다시 검사하고 밖은 버린다. 착수 정보(declare_context)는 첫 편집 전에만
- 실행 시간은 events(gate_time)에 남긴다
- 검증 명령(테스트·빌드·린트·타입체크·git commit 훅 근사) 소요는 gates.verify_timing이 events(verify_run)에 남긴다.
  성공한 테스트·브라우저 화면 캡처 실행은 gates.auto_evidence가 증거 마커로 남긴다(완료 게이트).
  PostToolUseFailure(Claude — 실패한 도구 호출은 PostToolUse 대신 이것만 온다, 2.1.308 실측)는 그 기록만 하고 다른 핸들러는 부르지 않는다
"""

import importlib
import json
import os
import re
import time

from vmc_harness.core import events, inject, paths
from vmc_harness.core.llm import nested
from vmc_harness.hooks import adapters, contract
from vmc_harness.hooks.contract import HookResult

_TAG = re.compile(r"^\[vmc-harness:([a-z_]+)\] (.*)$", re.S)
SAFE_FALLBACK_REASON = "하네스 안전 게이트 확인 실패 — 막음"
BLOCKABLE = ("PreToolUse", "Stop")


def _in_work(ctx):
    """작업 중인가(첫 편집 이후) — 착수 정보 주입 허용 판정."""
    try:
        from vmc_harness.task import state as tstate
        t = tstate.current(ctx.root, ctx.session_id)
    except Exception:
        t = None
    if t is not None:
        return bool(t.get("first_edit_at"))
    return ctx.event not in ("SessionStart", "UserPromptSubmit")


def valid_message(s, allow_declare):
    """core.inject.message()로 만든 문구인지 다시 검사. (ok, kind)"""
    if not isinstance(s, str):
        return False, None
    m = _TAG.match(s)
    if not m:
        return False, None
    kind, body = m.group(1), m.group(2)
    if kind == "declare_context" and not allow_declare:
        return False, kind
    if inject.message(kind, body) != s:
        return False, kind
    return True, kind


def _import(mod_name):
    try:
        return importlib.import_module(mod_name)
    except ImportError as e:
        # 그 모듈 자체가 아직 없으면 건너뛴다. 모듈 안의 import 오류는 예외로 올린다
        if getattr(e, "name", None) and mod_name.startswith(e.name):
            return None
        raise


def run(ctx, modules=None):
    """핸들러들을 부르고 최종 HookResult(필터 전)와 측정치."""
    modules = modules if modules is not None else contract.HANDLER_MODULES
    fname = contract.HANDLER_NAMES[ctx.event]
    injects, notices_, timings = [], [], {}
    final = None
    for mod_name, safety in modules:
        t0 = time.time()
        try:
            mod = _import(mod_name)
            fn = getattr(mod, fname, None) if mod else None
            r = fn(ctx) if fn else None
        except Exception as e:
            timings[mod_name] = round((time.time() - t0) * 1000.0, 1)
            _log(ctx, "gate_error", module=mod_name, event=ctx.event, error=("%s: %s" % (type(e).__name__, e))[:300])
            if safety and ctx.event in BLOCKABLE and not (ctx.event == "Stop" and ctx.raw.get("stop_hook_active")):
                final = HookResult(block=True, reason=inject.message("gate_violation", SAFE_FALLBACK_REASON))
                ctx.state["blocked_by"] = mod_name
                break
            continue
        if fn is not None:
            timings[mod_name] = round((time.time() - t0) * 1000.0, 1)
        if r is None:
            continue
        injects.extend(r.inject or [])
        if r.notice:
            notices_.append(r.notice)
        if r.block:
            final = r
            ctx.state["blocked_by"] = mod_name
            break
    if final is None:
        final = HookResult()
    final.inject = injects if not final.block else []
    final.notice = " / ".join(notices_) if notices_ else final.notice
    return final, timings


def filter_result(ctx, result):
    """01 P5 — 허용 목록 밖 주입은 버린다. 막는 사유도 같은 검사(안 맞으면 고정 문구로 — 막기는 유지)."""
    allow_declare = not _in_work(ctx)
    kept, dropped = [], []
    for s in result.inject or []:
        ok, kind = valid_message(s, allow_declare)
        (kept if ok else dropped).append(s if ok else (kind or "untagged"))
    result.inject = kept
    if result.block:
        ok, kind = valid_message(result.reason, False)
        if not ok:
            dropped.append("reason:%s" % (kind or "untagged"))
            result.reason = inject.message("gate_violation", "하네스 게이트가 막음")
    if dropped:
        _log(ctx, "inject_dropped", event=ctx.event, kinds=dropped)
    return result


def _with_unblock_hint(ctx, reason):
    """사람이 대화로 풀 수 있는 차단이면 사유 끝에 사람에게 전할 한 줄(gates.proceed.unblock_hint) — 사람이 탈출구를 모르면
    헛막기 기록("그냥 진행")이 0이 된다(현장 실측). 붙일 수 없으면 사유 그대로."""
    try:
        from vmc_harness.gates import proceed
        hint = proceed.unblock_hint(ctx, ctx.state.get("recorded_block_gate"))
        m = _TAG.match(reason or "")
        if not hint or not m:
            return reason
        return inject.message(m.group(1), m.group(2) + "\n" + hint) or reason
    except Exception:
        return reason


def _log(ctx, kind, **fields):
    try:
        events.log(ctx.root, kind, session_id=ctx.session_id, agent=ctx.agent, **fields)
    except Exception:
        pass


RECORD_ONLY = ("PostToolUseFailure",)   # 기록만 하는 이벤트(contract.EVENTS 밖 — 핸들러 계약을 넓히지 않는다)


def _verify_timing(ctx, event, result=None):
    """검증 명령 소요 기록 — 편의 기록이라 어떤 오류도 훅 결과를 바꾸지 않는다."""
    try:
        from vmc_harness.gates import verify_timing
        if event == "PreToolUse":
            if not (result and result.block):
                verify_timing.on_pre(ctx)
        elif event in ("PostToolUse", "PostToolUseFailure"):
            rec = verify_timing.on_post(ctx, failed=(event == "PostToolUseFailure"))
            if event == "PostToolUse":
                # 마지막 편집 뒤 이미 돌린 테스트·화면 캡처를 증거로(gates.auto_evidence) — 실패한 실행은 아니다
                from vmc_harness.gates import auto_evidence
                auto_evidence.on_post(ctx, rec)
    except Exception as e:
        _log(ctx, "gate_error", module="gates.verify_timing", event=event, error=("%s: %s" % (type(e).__name__, e))[:300])


def _record_only(event, raw, root, agent):
    adapter = adapters.get(agent)
    ctx = adapter.normalize("PostToolUse", raw, root)
    try:
        from vmc_harness.task import state as tstate
        ctx.task_id = (tstate.current(ctx.root, ctx.session_id) or {}).get("task_id")
    except Exception:
        pass
    _verify_timing(ctx, event)
    try:
        # 실패한 명령도 파일을 바꿨을 수 있다 — 해석 못 한 셸 명령의 실행 뒤 대조(되돌림·차단 기록). 출력은 내지 않는다
        # (PostToolUseFailure 출력 형식 미실측 — 사람에게는 꼭 알릴 일로 남는다)
        from vmc_harness.gates import handlers as kernel
        kernel.after_opaque(ctx)
    except Exception as e:
        _log(ctx, "gate_error", module="gates.opaque_watch", event=event, error=("%s: %s" % (type(e).__name__, e))[:300])
    return "", 0


def dispatch(event, agent_arg, stdin_text, modules=None, cwd=None):
    """(stdout 문자열, exit code)."""
    if nested() or (event not in contract.EVENTS and event not in RECORD_ONLY):
        return "", 0
    t0 = time.time()
    try:
        raw = json.loads(stdin_text) if (stdin_text or "").strip() else {}
        if not isinstance(raw, dict):
            raise ValueError("not an object")
        parse_error = False
    except ValueError:
        raw, parse_error = {}, True
    start = raw.get("cwd") or cwd or os.getcwd()
    root = paths.find_project_root(start)
    if root is None:
        return "", 0
    agent = adapters.detect_agent(agent_arg, raw)
    adapter = adapters.get(agent)
    if event in RECORD_ONLY:
        if parse_error:
            return "", 0
        try:
            return _record_only(event, raw, root, agent)
        except Exception:
            return "", 0
    if parse_error:
        if event == "PreToolUse":
            r = HookResult(block=True, reason=inject.message("gate_violation", "훅 입력 판독 불가 — 막음"))
            return adapter.render(event, r)
        return "", 0
    try:
        ctx = adapter.normalize(event, raw, root)
    except Exception as e:
        # 정규화 실패 — 안전 판정을 못 하므로 PreToolUse는 막는다
        try:
            events.log(str(root), "gate_error", module="adapter", event=event, error=str(e)[:300])
        except Exception:
            pass
        if event == "PreToolUse":
            return adapter.render(event, HookResult(block=True, reason=inject.message("gate_violation", SAFE_FALLBACK_REASON)))
        return "", 0
    try:
        from vmc_harness.task import state as tstate
        # 도구 활동이면 이 세션의 조용해진·세션 종료로 마감된 작업을 다시 연다(게이트가 같은 작업을 보게 — task.lifecycle)
        t = tstate.current(ctx.root, ctx.session_id, reopen=(event == "PreToolUse"))
        ctx.task_id = (t or {}).get("task_id")
    except Exception:
        pass
    result, timings = run(ctx, modules)
    if result.block:
        result.reason = _with_unblock_hint(ctx, result.reason)
    result = filter_result(ctx, result)
    _verify_timing(ctx, event, result)
    total_ms = (time.time() - t0) * 1000.0
    # 생성 게이트 시간은 게이트별 gate_time으로 따로 남는다(ledger.metrics 합산 — 두 번 세지 않게 뺀다)
    own_sec = max(total_ms - float(ctx.state.get("generated_ms") or 0), 0.0) / 1000.0
    # total_sec: 생성 게이트까지 합한 이 훅 호출 전체(작업별 하네스 오버헤드·가벼움 예산 실측이 읽는다)
    _log(ctx, "gate_time", gate_id="dispatcher:%s" % event, sec=round(own_sec, 4), total_sec=round(total_ms / 1000.0, 4),
         task_id=ctx.task_id, tool=ctx.tool_name,
         modules=timings, blocked_by=ctx.state.get("blocked_by"))
    return adapter.render(event, result)


def main(event, agent_arg=None, stdin=None):
    import sys
    if stdin is None:
        data = sys.stdin.buffer.read() if hasattr(sys.stdin, "buffer") else sys.stdin.read().encode("utf-8")
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
    else:
        text = stdin
    out, rc = dispatch(event, agent_arg, text)
    if out:
        sys.stdout.write(out)
        sys.stdout.flush()
    return rc
