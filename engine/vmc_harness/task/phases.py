"""작업 구간의 에이전트 시간을 단계로 나눈다(spec 13 §실측을 단계로 쪼갠다).

- 호출 시작~결과는 그 호출의 단계, 앞 결과~이 호출 사이(생각 시간)도 이 호출의 단계, 마지막 결과~턴 끝은 `other`
- 단계는 명령 내용으로 가른다. 도구 이름만으로 하지 않는다 — 2026-11-05 실측: 스펙 편집의 쓰기 호출이
  Bash 61·Write 17·Agent 16·Edit 0이었다(셸 heredoc·스크립트로 쓴다)
- `rework` = 두 번째 이후 사람 메시지 뒤, 그 메시지 전에 이미 고친 파일을 다시 고친 `edit`
- `answer` = 턴의 마지막 결과~턴 끝(답 쓰기). `other`는 분류 못 한 호출(하네스·계획 도구 포함)
- 셸 명령은 쓰기·검증 표지가 없고 알려진 비읽기 명령(커밋·푸시·설치 등)이 아니면 조사로 본다. 분류는 잘리기 전 전체
  명령으로(세션 파서가 `phase`를 미리 붙인다 — 긴 heredoc 끝의 write_text를 놓치지 않게)
"""

import re

from vmc_harness.sessions.common import ts

PHASES = ("explore", "edit", "verify", "rework", "delegate", "answer", "other")

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit", "apply_patch"}
_EXPLORE_TOOLS = {"Read", "Grep", "Glob", "LS", "WebFetch", "WebSearch", "NotebookRead", "view_image"}
_DELEGATE_TOOLS = {"Agent", "Task"}
_SHELL_TOOLS = {"Bash", "shell", "exec_command", "local_shell", "PowerShell"}

_VERIFY_RE = re.compile(
    r"\b(pytest|unittest|swift\s+(test|build)|xcodebuild|(npm|pnpm|yarn|bun)\s+(run\s+)?(test|build|lint|check|typecheck)"
    r"|npx\s+(tsc|vitest|jest|playwright|eslint)|tsc\b|vitest|jest\b|go\s+(test|build|vet)|cargo\s+(test|build|check|clippy)"
    r"|gradlew?\s|mvn\s|make\s+(test|check|build)|ruff\b|mypy\b|eslint\b|check_\w+\.py|ctest\b|dotnet\s+(test|build)"
    r"|git\s+hook\s+run)")
_WRITE_RE = re.compile(
    r"(^|[;&|(]\s*)(cat|tee|printf|echo)\b[^|;&]*>\s*[^&\s]|<<-?\s*'?\w+'?[^\n]*>|>\s*[\w./~-]+\.\w+|\bsed\s+-i|\bperl\s+-p?i"
    r"|\bapply_patch\b|\bpatch\s+-p|\bgit\s+(apply|mv|rm)\b|\b(mv|cp|rm|mkdir|touch)\s|write_text\(|\.write\(|open\([^)]*['\"][wa]")
_NOT_READ_RE = re.compile(
    r"\bgit\s+(commit|push|pull|merge|rebase|fetch|clone|checkout|switch|stash|tag|reset|worktree|cherry-pick|revert)\b"
    r"|\b(npm|pnpm|yarn|pip3?|brew|gem|cargo|go)\s+(install|add|i)\b|(^|[;&|]\s*)(open|sleep|kill|pkill|launchctl|caffeinate)\s"
    r"|\bcurl\b[^|;&]*-X\s*(POST|PUT|PATCH|DELETE)|\b(next|vite|uvicorn|flask)\s+(dev|run)\b")
_MCP_READ_RE = re.compile(r"(search|read|get|list|find|fetch|query|context)", re.I)


def _cmd(call):
    inp = call.get("input") or {}
    c = inp.get("command") if isinstance(inp, dict) else None
    if isinstance(c, list):
        c = " ".join(str(x) for x in c)
    return str(c or (inp.get("cmd") if isinstance(inp, dict) else "") or "")


def classify(call):
    """도구 호출 하나 → explore|edit|verify|delegate|other (rework는 compute가 edit에서 가른다)."""
    name = call.get("name") or ""
    if name in _DELEGATE_TOOLS:
        return "delegate"
    if name in _EDIT_TOOLS:
        return "edit"
    if name in _EXPLORE_TOOLS:
        return "explore"
    if name in _SHELL_TOOLS:
        cmd = _cmd(call)
        if _VERIFY_RE.search(cmd):
            return "verify"
        if _WRITE_RE.search(cmd):
            return "edit"
        if not _NOT_READ_RE.search(cmd):
            return "explore"
        return "other"
    if name.startswith("mcp__") and _MCP_READ_RE.search(name.rsplit("__", 1)[-1]):
        return "explore"
    return "other"


def compute(session):
    """Session(turns·tool_calls·human_prompts) → ({단계: 초}, {"explore_late": 초, "other_calls": n, "calls": n}).
    turns가 없으면 (None, None) — 틀린 숫자 대신 미측정."""
    turns = sorted(t for t in (getattr(session, "turns", None) or []) if t[0] and t[1])
    if not turns:
        return None, None
    calls = sorted((c for c in session.tool_calls or [] if ts(c.get("at"))), key=lambda c: ts(c["at"]))
    prompts = sorted(p for p in (ts(x.get("at")) for x in session.human_prompts or []) if p)
    out = {p: 0.0 for p in PHASES}
    late = 0.0
    other_calls = 0
    edited, snapshot, first_edit = set(), set(), None
    pi = 0
    for start, end in turns:
        cur = start
        for c in calls:
            at = ts(c["at"])
            if at < start or at > end:
                continue
            while pi < len(prompts) and prompts[pi] <= at:  # 이 호출 전에 온 사람 메시지 — 두 번째부터 정정 후보
                if pi >= 1:
                    snapshot = set(edited)
                pi += 1
            fin = ts(c.get("end")) or at
            fin = min(max(fin, at), end)
            span = max(0.0, (fin - cur).total_seconds()) if fin > cur else 0.0
            ph = c.get("phase") or classify(c)
            paths = set(c.get("paths") or [])
            if ph == "edit":
                if paths & snapshot:
                    ph = "rework"
                edited |= paths
                first_edit = first_edit or at
            elif ph == "explore" and first_edit and at > first_edit:
                late += span
            elif ph == "other":
                other_calls += 1
            out[ph] += span
            if fin > cur:
                cur = fin
        if end > cur:
            out["answer"] += (end - cur).total_seconds()
    return ({k: round(v, 1) for k, v in out.items()},
            {"explore_late": round(late, 1), "other_calls": other_calls, "calls": len(calls)})
