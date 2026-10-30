"""LLM 호출 — `claude -p` headless (spec 04 §검색, 03 PM).

- 훅 재귀 방지: --setting-sources "" 그리고 환경변수 VMC_HARNESS_NESTED=1 (훅이 이 값을 보면 아무것도 안 한다)
- 평가 때만 모델 교체: VMC_HARNESS_EVAL_MODEL (spec 11). 운영 코드는 모델 이름을 명시한 그대로
- 실패·시간 초과·로그인 없음이면 None — 호출자는 대체 경로(BM25 등)로 간다(편의 장치는 fail-open)
"""

import json
import os
import shutil

from vmc_harness.core.proc import run


def available():
    return shutil.which("claude") is not None


def claude_p(prompt, model="haiku", system=None, timeout=60, tools=False, cwd=None):
    """{'text': str, 'tokens': int, 'sec': float} 또는 None."""
    exe = shutil.which("claude")
    if not exe:
        return None
    model = os.environ.get("VMC_HARNESS_EVAL_MODEL") or model
    args = [exe, "-p", "--model", model, "--output-format", "json",
            "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence"]
    if not tools:
        args += ["--tools", ""]
    sp_file = None
    if system:
        # 지시문은 파일로 넘긴다 — Windows에서 한글·따옴표·줄바꿈이 든 긴 지시문을 명령줄 인자로 넘기면 깨진 채 받는 것으로
        # 보였다(실기 검증 2026-10-30: 맥에서 4/4 찾는 카드를 Windows에선 못 찾음). 표준 입력에 섞으면 품질이 떨어진다(맥 실측 0/1).
        import tempfile
        fd, sp_file = tempfile.mkstemp(prefix="vmh-sys-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(system)
        args += ["--system-prompt-file", sp_file]
    env = dict(os.environ)
    env["VMC_HARNESS_NESTED"] = "1"
    import time
    t0 = time.time()
    try:
        rc, out, _ = run(args, input_text=prompt, timeout=timeout, env=env, cwd=cwd)
    finally:
        if sp_file:
            try:
                os.unlink(sp_file)
            except OSError:
                pass
    sec = time.time() - t0
    if rc != 0 or not out.strip():
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return {"text": out.strip(), "tokens": 0, "sec": sec}
    if data.get("is_error"):
        return None
    usage = data.get("usage") or {}
    tokens = sum(int(usage.get(k) or 0) for k in
                 ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return {"text": (data.get("result") or "").strip(), "tokens": tokens, "sec": sec}


def nested():
    return os.environ.get("VMC_HARNESS_NESTED") == "1"


def claude_agent(prompt, model, system=None, timeout=900, tools=("Read", "Grep", "Glob", "Bash"),
                 allowed=None, cwd=None, add_dirs=None, env_extra=None):
    """도구를 쓰는 headless 호출(시니어식 진단 등). 쓰기 도구는 주지 않는다.

    tools: 쓸 수 있는 내장 도구 목록. allowed: 허락 없이 실행되는 도구·명령 패턴(예 'Bash(git log:*)').
    --permission-mode dontAsk → allowed 밖 호출은 자동 거부(읽기 전용 유지).
    {'text','tokens','sec','cost_usd','turns'} 또는 None.
    """
    exe = shutil.which("claude")
    if not exe:
        return None
    model = os.environ.get("VMC_HARNESS_EVAL_MODEL") or model
    args = [exe, "-p", "--model", model, "--output-format", "json",
            "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
            "--permission-mode", "dontAsk", "--tools", ",".join(tools)]
    if allowed:
        args += ["--allowedTools"] + list(allowed)
    args += ["--disallowedTools", "Edit", "Write", "NotebookEdit", "MultiEdit"]
    for d in add_dirs or []:
        args += ["--add-dir", str(d)]
    if system:
        # Windows: 한글·따옴표·줄바꿈이 든 긴 지시문을 명령줄 인자로 넘기면 claude가 깨진 채 받는 것으로 보인다
        # (실기 검증 2026-10-30 — 맥에서 4/4 찾는 카드를 Windows에선 못 찾음). 표준 입력 앞에 붙여 보낸다.
        how = os.environ.get("VMC_HARNESS_LLM_SYSTEM") or ("stdin" if sys.platform == "win32" else "arg")
        if how == "stdin":
            prompt = system + "\n\n---\n\n" + (prompt or "")
        else:
            args += ["--system-prompt", system]
    env = dict(os.environ)
    env["VMC_HARNESS_NESTED"] = "1"
    env.update(env_extra or {})
    import time
    t0 = time.time()
    rc, out, err = run(args, input_text=prompt, timeout=timeout, env=env, cwd=cwd)
    sec = time.time() - t0
    if not out.strip():
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return {"text": out.strip(), "tokens": 0, "sec": sec, "cost_usd": None, "turns": None}
    if data.get("is_error") and not data.get("result"):
        return None
    usage = data.get("usage") or {}
    tokens = sum(int(usage.get(k) or 0) for k in
                 ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return {"text": (data.get("result") or "").strip(), "tokens": tokens, "sec": sec,
            "cost_usd": data.get("total_cost_usd"), "turns": data.get("num_turns"),
            "timed_out": rc == -9}
