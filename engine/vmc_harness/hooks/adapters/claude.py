"""Claude Code 어댑터 (2.1.301 실측 — docs/verification/codex-hooks.md).

- 입력: Write/Edit/MultiEdit(file_path), NotebookEdit(notebook_path), Bash(command)
- 차단: PreToolUse `hookSpecificOutput.permissionDecision=deny` + `permissionDecisionReason`, Stop `{"decision":"block","reason"}`
- 주입: SessionStart·UserPromptSubmit·PreToolUse·PostToolUse `hookSpecificOutput.additionalContext`
- 사람용 한 줄: `systemMessage`(모델 컨텍스트에 안 들어감 — 실측)
"""

from vmc_harness.hooks.adapters import common

NAME = "claude"


def normalize(event, raw, root):
    return common.build(event, NAME, raw, root)


def render(event, result):
    return common.output(event, result)
