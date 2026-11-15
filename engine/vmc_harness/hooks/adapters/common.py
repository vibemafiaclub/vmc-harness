"""에이전트 공통 정규화 — 훅 입력 JSON → HookContext (hooks/contract.py).

입력 필드 실측(2026-10-22, docs/verification/codex-hooks.md): Claude Code 2.1.301과 Codex 0.146.0 모두
session_id·transcript_path·cwd·hook_event_name·tool_name·tool_input·tool_use_id·prompt·stop_hook_active를 같은 이름으로 준다.
차이: Codex 파일 편집 도구는 `apply_patch`(tool_input.command = 패치 본문), Claude는 Write/Edit/MultiEdit/NotebookEdit(file_path).
"""

import os
import re

from vmc_harness.gates import globs, shell
from vmc_harness.hooks.contract import HookContext

FILE_TOOLS = {"Write": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
SHELL_TOOLS = {"Bash", "shell", "exec_command", "local_shell", "unified_exec", "PowerShell", "container.exec"}
_PATCH_LINE = re.compile(r"^\*\*\* (Add|Update|Delete) File: (.+?)\s*$|^\*\*\* Move to: (.+?)\s*$", re.M)


def detect_agent(arg, raw):
    if arg in ("claude", "codex"):
        return arg
    tp = (raw or {}).get("transcript_path") or (raw or {}).get("agent_transcript_path") or ""
    if "/.codex/" in tp.replace("\\", "/"):
        return "codex"
    return "claude"


def parse_patch(text):
    """apply_patch 본문 → [{'op': add|update|delete|move, 'path': 원문 경로}]"""
    ops = []
    for m in _PATCH_LINE.finditer(text or ""):
        if m.group(1):
            ops.append({"op": m.group(1).lower(), "path": m.group(2)})
        else:
            ops.append({"op": "move", "path": m.group(3)})
    return ops


def _shell_command(tool_input):
    cmd = tool_input.get("command") if isinstance(tool_input, dict) else None
    if isinstance(cmd, list):
        try:
            import shlex
            return shlex.join([str(c) for c in cmd])
        except Exception:
            return " ".join(str(c) for c in cmd)
    return cmd if isinstance(cmd, str) else None


def build(event, agent, raw, root, tool_alias=None):
    """HookContext를 만든다. root는 하네스 설치 프로젝트 루트(없으면 None)."""
    raw = raw or {}
    cwd = raw.get("cwd") or os.getcwd()
    tool_name = raw.get("tool_name")
    if tool_alias and tool_name in tool_alias:
        tool_name = tool_alias[tool_name]
    tool_input = raw.get("tool_input") if isinstance(raw.get("tool_input"), dict) else {}
    ctx = HookContext(event=event, agent=agent, raw=raw, root=str(root) if root else None,
                      session_id=raw.get("session_id"), transcript_path=raw.get("transcript_path"),
                      cwd=cwd, tool_name=tool_name, tool_input=tool_input,
                      prompt=raw.get("prompt") if event == "UserPromptSubmit" else None)
    ops = []   # {'op','abs','rel'}
    if tool_name in FILE_TOOLS:
        p = tool_input.get(FILE_TOOLS[tool_name])
        if p:
            ops.append({"op": "write" if tool_name == "Write" else "edit", "raw": p})
    elif tool_name == "apply_patch":
        body = tool_input.get("command") or tool_input.get("patch") or tool_input.get("input") or ""
        if isinstance(body, list):
            body = "\n".join(str(x) for x in body)
        for o in parse_patch(body):
            ops.append({"op": {"add": "write", "update": "edit", "delete": "delete", "move": "write"}[o["op"]], "raw": o["path"]})
    elif tool_name in SHELL_TOOLS:
        cmd = _shell_command(tool_input)
        if cmd:
            ctx.command = cmd
            ctx.tool_name = "Bash"
            a = shell.analyze(cmd, cwd=cwd)
            ctx.state["shell_opaque"] = a["opaque"]
            ctx.state["shell_opaque_cmds"] = a.get("opaque_cmds") or []
            for p in a["writes"]:
                ops.append({"op": "shell_write", "raw": p})
            for p in a["deletes"]:
                ops.append({"op": "delete", "raw": p})
    for o in ops:
        rawp = os.path.expandvars(os.path.expanduser(str(o["raw"])))
        if os.sep == "/":
            rawp = rawp.replace("\\", "/")   # PowerShell·cmd 경로(.harness\ledger\x)도 같은 파일로 본다
        o["abs"] = str(globs.abspath(rawp, cwd))
        o["rel"] = globs.rel_to(root, o["abs"]) if root else None
    ctx.state["file_ops"] = ops
    ctx.state["write_abs"] = [o["abs"] for o in ops]
    seen = []
    for o in ops:
        if o["rel"] and o["rel"] not in seen:
            seen.append(o["rel"])
    ctx.write_paths = seen
    return ctx


def output(event, result):
    """HookResult → (stdout 문자열, exit code). Claude·Codex 실측 형식이 같다(2026-10-22)."""
    import json
    if result is None:
        return "", 0
    out = {}
    if result.block:
        reason = result.reason or "[vmc-harness:gate_violation] 차단"
        if event == "PreToolUse":
            out["hookSpecificOutput"] = {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                         "permissionDecisionReason": reason}
        elif event in ("Stop", "UserPromptSubmit", "PostToolUse"):
            out["decision"] = "block"
            out["reason"] = reason
    elif result.inject and event in ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse"):
        out["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": "\n".join(result.inject)}
    if result.notice:
        out["systemMessage"] = result.notice   # 사람에게만 보인다(모델 컨텍스트 밖 — 실측)
    if not out:
        return "", 0
    return json.dumps(out, ensure_ascii=False), 0
