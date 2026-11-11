"""차단 사유에 사람이 대화로 푸는 한 줄 (spec 02 §"그냥 진행"·01 P5).

현장 실측: 차단 메시지가 탈출구를 말하지 않아 사람이 "그냥 진행"을 한 번도 쓰지 않았고, 에이전트는 터미널 명령을 대신 안내했다.
받는 게이트(gates.proceed_allowed)만 붙인다. 엔진 쓰기 보호·DB 직접 접속은 붙이지 않는다.
"""

from tests.gates_util import GatesCase
from tests.test_gates_safety import TaskMixin
from vmc_harness.hooks import dispatcher


def _reason(out):
    return (out.get("hookSpecificOutput") or {}).get("permissionDecisionReason") or out.get("reason") or ""


class TestUnblockHint(TaskMixin, GatesCase):
    def setUp(self):
        super().setUp()
        self.write("tests/test_calc.py", "def test_a():\n    assert 1\n")
        self.write("fe/pages/settle.tsx", "export const A = 1\n")
        self.commit()
        self.hook("SessionStart", source="startup")

    def test_proceed_allowed_gate_has_hint_and_passes_filter(self):
        self.set_task()
        out = self.hook("PreToolUse", "Edit", {"file_path": str(self.root / "tests/test_calc.py"),
                                               "old_string": "assert 1", "new_string": "assert 0"})
        self.assertDenied(out, "검증물 변조")
        r = _reason(out)
        self.assertTrue(r.startswith("[vmc-harness:gate_violation] "), r)
        self.assertIn("'그냥 진행'", r)
        self.assertIn("그대로 전한다", r)
        ok, _ = dispatcher.valid_message(r, False)
        self.assertTrue(ok)
        # 그 문구로 실제로 한 번 풀린다
        self.hook("UserPromptSubmit", prompt="그냥 진행")
        self.assertAllowed(self.hook("PreToolUse", "Edit", {"file_path": str(self.root / "tests/test_calc.py"),
                                                            "old_string": "assert 1", "new_string": "assert 0"}))

    def test_completion_stop_has_hint(self):
        self.set_task()
        self.write("fe/pages/settle.tsx", "export const A = 2\n")
        out = self.hook("Stop", stop_hook_active=False)
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("'그냥 진행'", out["reason"])

    def test_not_allowed_gates_have_no_hint(self):
        out = self.hook("PreToolUse", "Write", {"file_path": str(self.root / ".harness/ledger/x.json"), "content": "{}"})
        self.assertDenied(out)
        self.assertNotIn("그냥 진행", _reason(out))
        self.assertNotIn("승인", _reason(out))
