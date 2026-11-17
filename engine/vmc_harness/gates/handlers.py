"""커널 안전 게이트 훅 핸들러 (contract.HANDLER_MODULES 첫 번째, safety=True — 예외면 디스패처가 막는다).

PreToolUse: 엔진 쓰기 보호 → DB 직접 접속 차단 → 운영 접속 설정 읽기·실행(gates.prod_config) → 메타 게이트(L1 풀기) → 보호 영역 → 검증물 변조
  해석 못 한 셸 명령(opaque)은 먼저 대상 파일 상태를 적어 두고(gates.opaque_watch) 미리 막지 않는다
PostToolUse(·PostToolUseFailure — 디스패처 기록 전용 경로): opaque 명령이 장부·증거·보호 영역·경로형 생성 게이트 대상을 바꿨으면 되돌리고 차단
Stop: 메타 게이트(작업 트리 L1 diff) → 완료 게이트
UserPromptSubmit: "그냥 진행"(헛막기 finding)
PostToolUse·UserPromptSubmit: 착수 선언 protected_touch면 작업 시작 승인 카드 한 번
SessionStart: 세션 기준점(HEAD·시작 시각) 기록
착수 선언 존재 확인은 task 영역 핸들러가 한다(여기서 하지 않음).
"""

import hashlib
import json
import os

from vmc_harness.core import config
from vmc_harness.core.clock import now_iso
from vmc_harness.core.gitutil import head
from vmc_harness.gates import engine_write_guard, meta, opaque_watch, prod_config, protected, shell, state, tamper
from vmc_harness.gates.common import block, log_event, notify
from vmc_harness.hooks.contract import HookResult

NAME = "kernel"


def _blocked(ctx, gate, result, gate_id=None):
    if result is None or not result.block:
        return result
    gate_id = gate_id or ("kernel:%s" % gate)
    state.record_block(ctx.root, ctx, gate, result.reason, gate_id=gate_id)
    ctx.state["blocked_gate"] = gate
    try:
        log_event(ctx.root, "gate_block", gate=gate, gate_id=gate_id, event=ctx.event, tool=ctx.tool_name,
                   paths=list(ctx.write_paths or [])[:int(config.get("gates.reason_max_files", ctx.root))])
        key = "block:%s:%s" % (gate, state.fingerprint(ctx))
        if state.noticed_once(ctx.root, ctx.session_id, key):
            text = (result.reason or "").split("] ", 1)[-1]
            area = os.path.dirname(ctx.write_paths[0]) if ctx.write_paths else None
            notify(ctx.root, "gate_blocked", "must", text, area=area or None,
                        refs={"gate": gate, "session_id": ctx.session_id})
    except Exception:
        pass
    return result


def blocked(ctx, gate, result, gate_id=None):
    """다른 영역(db·qa) 차단도 같은 기록(차단 목록·gate_block·알림)을 남긴다 — 기록이 없으면 why·"그냥 진행"·헛막기 집계가 그 차단을 못 본다(현장 결함 C8)."""
    return _blocked(ctx, gate, result, gate_id=gate_id)


def _meta_key(viol):
    body = json.dumps(sorted(v["text"] for v in viol), ensure_ascii=False)
    return "meta_stop:%s" % hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def _meta_block(ctx, viol):
    """메타 게이트 차단 + 사람 승인으로 풀 수 있는 위반이면 승인 카드(대화창 알림)와 '승인' 대화 대기 기록."""
    r = _blocked(ctx, meta.NAME, block("메타 게이트: " + " / ".join(v["text"] for v in viol),
                                       "메타 게이트: L1 변경이 증거·승인 없이 안전 경계를 넓힘"))
    try:
        ids = meta.pending_cards(ctx.root, state.task_key(ctx.root, ctx.session_id, ctx.task_id), viol)
    except Exception:
        ids = []
    if ids:
        st = state.load(ctx.root, ctx.session_id)
        st["meta_pending"] = {"at": now_iso(), "approvals": ids}
        state.save(ctx.root, ctx.session_id, st)
        ctx.state["meta_approvable"] = True
        card = _card_notice(ctx.root, ids[0])
        if card:
            r.notice = card + "\n또는 대화창에 '%s'이라고만 답하면 승인됩니다" % config.get("gates.approve_phrases", ctx.root)[0]
    return r


DB_TEXT = "db_text"   # 실행하지 않는 문자열에만 접속 흔적 — "그냥 진행" 대상(gates.proceed_allowed)


def _db_direct(ctx):
    """(게이트 이름, HookResult) 또는 (None, None). 실행 근거가 있으면 db_direct, 문자열 근거만이면 db_text."""
    if not ctx.command:
        return None, None
    cmds = shell.analyze(ctx.command)["commands"]
    if cmds and all(c == "vmc-harness" for c in cmds):
        return None, None
    try:
        import importlib
        mod = importlib.import_module("vmc_harness.db.sqlguard")
        fn = getattr(mod, "direct_db_verdict", None)
        if fn is None:
            old = mod.is_direct_db_command
            fn = lambda c, cwd=None: ("DB 직접 접속 차단됨", "exec") if old(c, cwd=cwd) else None
    except (ImportError, AttributeError):
        # 안전 게이트 의존 — 판정 도구가 없으면 막는다(01 P8)
        return "db_direct", block("DB 직접 접속 판정 도구 없음 — 셸 명령 차단")
    try:
        hit = fn(ctx.command, cwd=ctx.cwd or ctx.root)
    except Exception:
        return "db_direct", block("DB 직접 접속 판정 실패 — 셸 명령 차단")
    if not hit:
        return None, None
    reason, kind = hit
    if kind == "text":
        return DB_TEXT, block("DB 직접 접속 의심(실행하지 않는 문자열에 접속 흔적) — %s" % reason,
                              "DB 직접 접속 의심(실행하지 않는 문자열에 접속 흔적)")
    return "db_direct", block("DB 직접 접속 차단됨")


def _card_notice(root, approval_id):
    """새로 만든 승인 카드를 대화창에서 사람에게 보일 글(systemMessage — 모델 컨텍스트 밖). 카드를 못 만들면 None.
    대화창·패널 어느 쪽에서 눌러도 같은 기록이 처리된다(spec 08)."""
    if not approval_id or approval_id == "-":
        return None
    try:
        from vmc_harness.render import approval_card
        c = approval_card.card(root, approval_id, allow_llm=False)
        approval_card.mark_shown(root, approval_id)
    except Exception:
        return None
    return "\n".join(list(c.get("lines") or []) + [
        "승인 · 거절 · 자세히: vmc-harness approval %s approve | reject | detail" % approval_id])


def _declared_card(ctx, task):
    """착수 선언 protected_touch → 작업 시작에 카드 한 번(만들어졌을 때만 사람에게 보인다)."""
    if not task:
        return None
    a = protected.ensure_declared_approval(ctx.root, task)
    text = _card_notice(ctx.root, (a or {}).get("approval_id"))
    return HookResult(notice=text) if text else None


def on_session_start(ctx):
    st = state.load(ctx.root, ctx.session_id)
    if not st.get("session_started_at"):
        st["session_started_at"] = now_iso()
        st["base_head"] = head(ctx.root)
        state.save(ctx.root, ctx.session_id, st)
    return None


def on_pre_tool(ctx):
    if opaque_watch.needed(ctx):
        ctx.state["opaque_watch"] = opaque_watch.take(ctx)
    r = _blocked(ctx, engine_write_guard.NAME, engine_write_guard.check(ctx))
    if r:
        return r
    gate, r = _db_direct(ctx)
    if r and not (gate == DB_TEXT and state.consume_bypass(ctx.root, ctx, DB_TEXT)):
        return _blocked(ctx, gate, r)
    r = prod_config.safe_check(ctx)
    if r and not state.consume_bypass(ctx.root, ctx, prod_config.NAME):
        return _blocked(ctx, prod_config.NAME, r)
    viol = meta.check_pre_items(ctx)
    if viol and not state.consume_bypass(ctx.root, ctx, meta.NAME):
        return _meta_block(ctx, viol)
    if not ctx.write_paths and not (ctx.command and ctx.state.get("shell_opaque")):
        return None
    task = state.current_task(ctx.root, ctx.session_id)
    declared = _declared_card(ctx, task)
    r = _blocked(ctx, protected.NAME, protected.check(ctx, task))
    if r:
        r.notice = _card_notice(ctx.root, ctx.state.get("approval_created")) or (declared.notice if declared else None)
        return r
    r = tamper.check(ctx, task)
    if r and not state.file_bypassed(ctx.root, ctx, tamper.NAME, ctx.state.get("block_paths"), task):
        return _blocked(ctx, tamper.NAME, r)
    return None


def after_opaque(ctx, task=None):
    """opaque 셸 명령 실행 뒤 대조 — 바뀌면 안 되는 파일이 바뀌었으면 되돌리고 차단으로 기록한 HookResult. 아니면 None."""
    if not opaque_watch.needed(ctx):
        return None
    task = task if task is not None else state.current_task(ctx.root, ctx.session_id)
    r, reverted, failed = opaque_watch.check(ctx, task)
    if not r:
        return None
    gate = ctx.state.get("opaque_gate")
    ctx.write_paths = list(reverted) + list(failed)
    log_event(ctx.root, "opaque_reverted", gate=gate, reverted=reverted[:int(config.get("gates.reason_max_files", ctx.root))],
              failed=failed, n=len(reverted))
    if gate == "generated":   # generated.evaluate가 이미 차단으로 기록했다
        return r
    r = _blocked(ctx, gate, r)
    if gate == protected.NAME:
        r.notice = _card_notice(ctx.root, ctx.state.get("approval_created"))
    return r


def on_post_tool(ctx):
    task = state.current_task(ctx.root, ctx.session_id)
    if ctx.write_paths:
        state.note_writes(ctx.root, ctx.session_id, ctx.write_paths)
    r = after_opaque(ctx, task)
    if r:
        return r
    if ctx.state.get("file_ops"):
        st = state.load(ctx.root, ctx.session_id)
        st["last_edit_at"] = now_iso()
        state.save(ctx.root, ctx.session_id, st)
    return _declared_card(ctx, task)


def on_user_prompt(ctx):
    from vmc_harness.gates import proceed
    try:
        f = proceed.from_prompt(ctx)
    except proceed.ProceedError:
        f = None
    if f:
        ctx.state["proceed_finding"] = f["finding_id"]
    approved = meta.approve_from_prompt(ctx)
    if approved:
        ctx.state["meta_approved"] = approved
        log_event(ctx.root, "meta_approved", via="prompt", approvals=approved)
    task = state.current_task(ctx.root, ctx.session_id)
    return _declared_card(ctx, task)


def on_stop(ctx):
    from vmc_harness.gates import completion   # Stop에서만 — 매 도구 호출 훅이 증거·QA 모듈까지 읽지 않게(E2)
    viol = meta.judge_worktree_items(ctx.root)
    # 이미 작업 트리에 있는 L1 변경은 (세션, 변경)마다 한 번만 종료를 막는다 — 고치는 행위는 PreToolUse가 막고,
    # 여기서 매번 막으면 파일을 안 고친 세션까지 끝나지 못한다(현장 결함: 두 세션 4분에 40회). 다음부터는 통과(알림은 _blocked가 1회)
    if viol and not state.consume_bypass(ctx.root, ctx, meta.NAME) and state.noticed_once(ctx.root, ctx.session_id, _meta_key(viol)):
        return _meta_block(ctx, viol)
    task = state.current_task(ctx.root, ctx.session_id)
    return _blocked(ctx, completion.NAME, completion.check(ctx, task))
