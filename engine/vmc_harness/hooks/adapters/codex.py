"""Codex 어댑터 (0.146.0 실측 — docs/verification/codex-hooks.md).

- 입력: 셸은 tool_name `Bash` + tool_input.command(문자열), 파일 편집은 `apply_patch` + tool_input.command(패치 본문)
  공통 필드 외에 model·turn_id·permission_mode가 온다. SessionEnd(reason)·UserPromptSubmit(prompt) 있음
- 차단: Claude와 같은 JSON(PreToolUse permissionDecision=deny, Stop decision=block). exit 2 + stderr도 막힌다
- 주입: SessionStart·UserPromptSubmit·PreToolUse·PostToolUse additionalContext가 모델에 들어간다
- 훅은 신뢰(trust) 등록이 있어야 실행된다 — hooks/install.py가 app-server로 등록한다
"""

from vmc_harness.hooks.adapters import common

NAME = "codex"
# 다른 이름으로 오는 셸 도구(버전에 따라) — 정규화
TOOL_ALIAS = {"shell": "Bash", "exec_command": "Bash", "local_shell": "Bash", "unified_exec": "Bash"}


def normalize(event, raw, root):
    return common.build(event, NAME, raw, root, tool_alias=TOOL_ALIAS)


def render(event, result):
    return common.output(event, result)
