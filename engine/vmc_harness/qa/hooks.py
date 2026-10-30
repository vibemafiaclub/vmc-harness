"""QA 쪽 PreToolUse 검사 — loop.handlers가 부른다(qa·loop 영역 핸들러 모듈 하나로 등록돼 있다).

1) 시나리오 파일 쓰기: 기대값 출처 메타 게이트(spec 05)
2) data-policy.json 쓰기: 범위 넓히기는 사람만(사람 개입 기준 2)
3) QA 진행 중: 범위 밖 쓰기(DB 직접 접근·금지 주소·개발 서버 밖 쓰기 요청) 차단
메시지는 무엇을 어겼는지만(01 P6).
"""

import json
from pathlib import Path

from vmc_harness.core import inject

SCENARIO_PREFIX = ".harness/qa/scenarios/"
POLICY_REL = ".harness/qa/data-policy.json"


def _new_content(root, rel, ctx):
    """이 도구 호출 뒤 파일 내용(가능할 때만). 못 구하면 None."""
    ti = ctx.tool_input or {}
    if ctx.tool_name == "Write":
        return ti.get("content")
    p = Path(root) / rel
    old = p.read_text(encoding="utf-8") if p.is_file() else ""
    if ctx.tool_name == "Edit":
        a, b = ti.get("old_string"), ti.get("new_string")
        if a is None or b is None:
            return None
        return old.replace(a, b) if ti.get("replace_all") else old.replace(a, b, 1)
    if ctx.tool_name == "MultiEdit":
        cur = old
        for e in ti.get("edits") or []:
            a, b = e.get("old_string"), e.get("new_string")
            if a is None or b is None:
                return None
            cur = cur.replace(a, b) if e.get("replace_all") else cur.replace(a, b, 1)
        return cur
    return None


def _block(reason):
    from vmc_harness.hooks.contract import HookResult
    msg = inject.message("gate_violation", reason)
    return HookResult(block=True, reason=reason, inject=[msg] if msg else [])


def on_pre_tool(ctx):
    root = ctx.root
    if not root:
        return None
    from vmc_harness.qa import policy, runner, scenarios
    own_cmd = (ctx.command or "").strip().startswith(("vmc-harness ", "vmc-harness.exe "))
    for rel in ([] if own_cmd else (ctx.write_paths or [])):
        rel = rel.replace("\\", "/")
        if rel.startswith(SCENARIO_PREFIX) and rel.endswith(".json"):
            content = _new_content(root, rel, ctx)
            if content is None:
                if ctx.tool_name in ("Bash", "apply_patch"):
                    return _block("시나리오 파일은 출처 검사를 거쳐야 함(셸·패치 쓰기)")
                continue
            ok, reason = scenarios.check_write(root, rel, content)
            if not ok:
                return _block(reason)
        if rel == POLICY_REL:
            content = _new_content(root, rel, ctx)
            if content is None:
                return _block("데이터 범위 설정은 셸·패치로 쓸 수 없음")
            try:
                new = json.loads(content)
            except ValueError:
                return _block("데이터 범위 설정 JSON 형식 오류")
            wide, why = policy.is_widening(policy.load(root), new)
            if wide:
                return _block("데이터 범위 넓히기(%s)는 사람 확인 대상" % why)
    if not runner.active(root):
        return None
    pol = policy.load(root) or dict(policy.DEFAULT_POLICY)
    if ctx.command:
        ok, reason = policy.check_write(root, {"kind": "command", "command": ctx.command}, pol)
        if not ok:
            return _block(reason)
    url = (ctx.tool_input or {}).get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        ok, reason = policy.check_write(root, {"kind": "url", "url": url}, pol)
        if not ok:
            return _block(reason)
    return None
