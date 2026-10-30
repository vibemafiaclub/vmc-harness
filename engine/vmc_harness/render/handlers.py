"""render 영역 훅 처리 (hooks/contract.py). 아무것도 막지 않는다(편의 장치 — fail-open). 주입은 term_context 하나뿐.

- SessionStart: 브리핑 캐시 갱신 + 그 주 성장 브리핑이 없으면 만들기 — 둘 다 백그라운드(세션 시작에 띄우지 않는다)
- UserPromptSubmit: 독자가 "'X'가 뭔 말이야" → 어휘집 갱신 후보(영향은 어휘집 안에서만)
  / "'X'는 Y를 말한 거야" → 모호어 후보 / 프롬프트에 나온 프로젝트 용어 뜻을 에이전트에게(term_context, lexicon.term_context)
- SessionEnd: 세션이 render.profile_recompute_sessions개 쌓이면 독자 프로필 재계산(백그라운드)
"""

import sys

from vmc_harness.core import config
from vmc_harness.core.jsonio import read_json, write_json


def _spawn(root, *args):
    from vmc_harness.core.proc import spawn_detached
    try:
        spawn_detached([sys.executable, "-m", "vmc_harness"] + list(args), cwd=str(root))
    except Exception:
        pass


def on_session_start(ctx):
    if not ctx.root:
        return None
    args = ["brief", "--refresh"]
    try:
        from vmc_harness.render import growth
        if not growth.weekly_path(ctx.root).is_file():
            args.append("--weekly")
    except Exception:
        pass
    _spawn(ctx.root, *args)
    return None


def on_user_prompt(ctx):
    if not ctx.root or not ctx.prompt:
        return None
    from vmc_harness.hooks.contract import HookResult
    from vmc_harness.render import lexicon
    word = lexicon.detect_unclear(ctx.prompt)
    if word:
        try:
            lexicon.unclear(ctx.root, word, source="prompt")
        except Exception:
            pass
    skip = ()
    fix = lexicon.detect_correction(ctx.prompt)
    if fix:
        skip = (fix[0],)  # 사람이 방금 뜻을 정해 준 말에 이전 정의를 덧붙이지 않는다
        try:
            lexicon.correct(ctx.root, fix[0], fix[1], source="prompt")
        except Exception:
            pass
    msg = lexicon.term_context(ctx.root, ctx.session_id, ctx.prompt, skip=skip)
    return HookResult(inject=[msg]) if msg else None


def _counter_path(root):
    from vmc_harness.core.project import local_dir
    return local_dir(root) / "render-state.json"


def on_session_end(ctx):
    if not ctx.root:
        return None
    p = _counter_path(ctx.root)
    st = read_json(p, {}) or {}
    st["sessions_since_profile"] = int(st.get("sessions_since_profile") or 0) + 1
    if st["sessions_since_profile"] >= config.get("render.profile_recompute_sessions", ctx.root):
        st["sessions_since_profile"] = 0
        _spawn(ctx.root, "render", "--refresh-profile")
    write_json(p, st)
    return None
