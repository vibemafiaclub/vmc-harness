"""착수 선언과 쓰기를 한 셸 명령에 보낸 경우 (spec 03 착수 선언, 2026-11-18 실측).

`vmc-harness declare …; <쓰기>`를 한 Bash로 보내면 PreToolUse 시점엔 아직 선언 전이라 통째로 막혔다(설치 뒤 22건).
훅이 앞 구간의 declare 인자를 직접 검증·등록하고 통과시킨다. 실측 22건 모두 `| tail`이라 종료 코드에 기댈 수 없다.
- 예측까지 다 있으면 통과, 빠지면 빠진 옵션 이름과 함께 막힘
- 인자에 변수·명령 치환이 있으면(값을 모름) 막힘, 쓰기가 선언보다 앞이면 막힘
- 명령이 실행되며 CLI가 같은 인자로 다시 부르면 같은 작업(--new-task여도)
픽스처는 합성이다.
"""

import os

from tests.gates_util import GatesCase
from vmc_harness.core import inject
from vmc_harness.task import declare as dec, state as st

WITH_TASK = [("vmc_harness.gates.handlers", True), ("vmc_harness.task.handlers", False)]
DECL = ('vmc-harness declare --intent "문의 기록" --scope "logs/q/README.md" '
        '--tokens 3000,5000,8000 --agent-min 1,2,4 --new-task 2>&1 | tail -3')


class TestInlineDeclare(GatesCase):
    def setUp(self):
        super().setUp()
        for k in st.SESSION_ENV:
            os.environ.pop(k, None)
        self.write("logs/q/README.md", "x\n")
        self.commit()
        self.hook("SessionStart", source="startup", modules=WITH_TASK)

    def bash(self, cmd):
        return self.hook("PreToolUse", "Bash", {"command": cmd}, modules=WITH_TASK)

    def test_declare_then_write_passes_and_registers(self):
        cmd = "cd %s; %s\nsed -i '' 's/x/y/' logs/q/README.md" % (self.root, DECL)
        self.assertAllowed(self.bash(cmd))
        t = st.current(self.root, self.session)
        self.assertIsNotNone(t)
        self.assertEqual(t["declare"]["intent"], "문의 기록")
        self.assertIn("logs/q/README.md", t["edits"])
        # 명령이 실행되며 CLI가 같은 인자로 다시 부름 — --new-task여도 같은 작업
        res = dec.declare(self.root, {"intent": "문의 기록", "scope": ["logs/q/README.md"],
                                      "predict": {"tokens": "3000,5000,8000", "agent_min": "1,2,4"}},
                          session_id=self.session, mode="new")
        self.assertEqual(res["task"]["task_id"], t["task_id"])
        self.assertIsNone(res["closed"])

    def test_missing_prediction_names_options(self):
        cmd = 'vmc-harness declare --intent "x" --scope "logs/q/README.md" | tail -3\necho y > logs/q/README.md'
        out = self.bash(cmd)
        self.assertDenied(out, "착수 선언 없음")
        reason = out["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("--tokens", reason)
        self.assertIn("--agent-min", reason)
        self.assertIsNone(st.current(self.root, self.session))

    def test_variable_argument_blocks(self):
        cmd = 'S=logs/q/README.md; vmc-harness declare --intent "x" --scope "$S" --tokens 1,2,3 --agent-min 1,2,3\necho y > logs/q/README.md'
        self.assertDenied(self.bash(cmd), "착수 선언 없음")
        cmd2 = 'vmc-harness declare --intent "$(date)" --scope "logs/q/README.md" --tokens 1,2,3 --agent-min 1,2,3\necho y > logs/q/README.md'
        self.assertDenied(self.bash(cmd2), "변수")
        self.assertIsNone(st.current(self.root, self.session))

    def test_write_before_declare_blocks(self):
        cmd = "echo y > logs/q/README.md; " + DECL
        self.assertDenied(self.bash(cmd), "앞에 다른 명령")
        self.assertIsNone(st.current(self.root, self.session))

    def test_unknown_option_blocks_with_sample(self):
        cmd = 'vmc-harness declare --intent "x" --scope "logs/q/README.md" --prediction "작음"\necho y > logs/q/README.md'
        self.assertDenied(self.bash(cmd), "--agent-min")

    def test_plain_block_message_has_sample_without_numbers(self):
        from vmc_harness.task import handlers
        msg = inject.message("gate_violation", handlers.NO_DECLARE_DETAIL)
        self.assertIsNotNone(msg)
        for opt in ("--intent", "--scope", "--tokens", "--agent-min"):
            self.assertIn(opt, msg)
        self.assertFalse(any(c.isdigit() for c in msg))

    def test_declare_error_lists_options(self):
        with self.assertRaises(dec.DeclareError) as cm:
            dec.normalize({"intent": "x", "scope": ["a"]})
        self.assertEqual(cm.exception.options, ["tokens", "agent"])
        self.assertIn("--tokens", str(cm.exception))
