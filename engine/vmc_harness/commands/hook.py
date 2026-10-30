"""`vmc-harness hook <event> --agent <claude|codex>` — 에이전트 훅 진입점(spec 02). stdin = 훅 입력 JSON.

어떤 경우에도 traceback으로 끝나지 않는다: 디스패처 밖 예외가 나면 PreToolUse는 막고(안전), 나머지는 통과.
"""

import json
import sys

from vmc_harness.hooks.contract import EVENTS


def register(sub):
    p = sub.add_parser("hook", help="에이전트 훅 진입점(설정 파일이 부른다) — stdin JSON")
    from vmc_harness.hooks.dispatcher import RECORD_ONLY
    p.add_argument("event", choices=tuple(EVENTS) + tuple(RECORD_ONLY))
    p.add_argument("--agent", choices=("claude", "codex"))
    p.set_defaults(func=run)


def run(args):
    try:
        from vmc_harness.hooks import dispatcher
        return dispatcher.main(args.event, args.agent)
    except Exception:
        if args.event == "PreToolUse":
            sys.stdout.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PreToolUse", "permissionDecision": "deny",
                "permissionDecisionReason": "[vmc-harness:gate_violation] 하네스 안전 게이트 확인 실패 — 막음"}},
                ensure_ascii=False))
        return 0
