""""그냥 진행" — 게이트가 잘못 막았을 때 사람의 명시적 행동 → finding(false_block) + 그 호출 1회 통과 (spec 00 기준 3·01 P2).

사람의 행동으로만 인정한다:
- 대화: 차단 직후(gates.proceed_window_min 안) 사람 입력(UserPromptSubmit)에 "그냥 진행" 등(gates.proceed_phrases)
- 터미널: `vmc-harness proceed` — 사람이 직접 친 경우만(표준 입력이 터미널). 에이전트의 셸 도구에서 부르면 거부한다
엔진 쓰기 보호·DB 직접 접속·보호 영역(승인 카드로 처리)은 대상이 아니다(gates.proceed_allowed).
"""

import sys

from vmc_harness.core import config, ledger, paths
from vmc_harness.core.jsonio import read_json
from vmc_harness.gates import state
from vmc_harness.gates.common import log_event


class ProceedError(ValueError):
    pass


def _allowed(gate):
    return gate in config.get("gates.proceed_allowed")


def _target(block):
    return {"gate_id": block.get("gate_id") or ("kernel:%s" % block["gate"])}


def grant(root, session_id, block, via):
    if block.get("proceeded"):
        raise ProceedError("이미 진행 처리된 차단")
    if not _allowed(block["gate"]):
        raise ProceedError("이 차단은 '그냥 진행' 대상이 아님: %s" % block["gate"])
    target = _target(block)
    if block.get("gate_id"):
        from vmc_harness.gates import generated
        m = generated.get(root, block["gate_id"])
        if m and m.get("action_id"):
            target["action_id"] = m["action_id"]
    f = ledger.create_finding(root, "false_block", target, source_event={"via": via, "blocked_at": block["at"],
                                                                         "gate": block["gate"]})
    state.grant_bypass(root, session_id, block)
    log_event(root, "proceed_anyway", gate=block["gate"], gate_id=block.get("gate_id"), via=via, finding_id=f["finding_id"])
    return f


def from_prompt(ctx):
    """UserPromptSubmit: 차단 직후 사람이 "그냥 진행"이라 했으면 finding + 1회 통과. 처리했으면 finding dict."""
    text = (ctx.prompt or "").strip().lower()
    if not text:
        return None
    phrases = [p.lower() for p in config.get("gates.proceed_phrases", ctx.root)]
    if not any(p in text for p in phrases):
        return None
    b = state.last_block(ctx.root, ctx.session_id)
    if not b or b.get("proceeded"):
        return None
    mins = state.minutes_since(b.get("at"))
    if mins is None or mins > float(config.get("gates.proceed_window_min", ctx.root)):
        return None
    if not _allowed(b["gate"]):
        return None
    return grant(ctx.root, ctx.session_id, b, via="prompt")


def latest_block(root):
    """모든 세션 중 가장 최근 차단 (session_id, block)."""
    d = paths.cache_dir(root) / "gates"
    best = (None, None)
    if not d.is_dir():
        return best
    for f in d.glob("session-*.json"):
        st = read_json(f, None) or {}
        for b in st.get("blocks") or []:
            if b.get("proceeded"):
                continue
            if best[1] is None or (b.get("at") or "") > (best[1].get("at") or ""):
                best = (f.stem[len("session-"):], b)
    return best


def from_cli(root, session_id=None, require_tty=True):
    if require_tty and not (sys.stdin and sys.stdin.isatty()):
        raise ProceedError("사람이 터미널에서 직접 실행한 경우만 인정")
    if session_id:
        b = state.last_block(root, session_id)
        sid = session_id
    else:
        sid, b = latest_block(root)
    if not b:
        raise ProceedError("진행할 차단 없음")
    return grant(root, sid, b, via="cli")
