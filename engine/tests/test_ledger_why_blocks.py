"""`why`가 차단을 보여준다 — 작업·파일마다 어느 게이트가 무엇 때문에 막았는지(spec 04, 현장 결함 C18).

현장: 차단 이벤트에 세션·작업 번호가 없어 `why <작업>`에 게이트가 안 나왔고, 나와도 횟수뿐이었다.
"""

from unittest import mock

from vmc_harness.core import events
from vmc_harness.ledger import why
from tests.gates_util import GatesCase
from tests.test_gates_safety import TaskMixin

T = "tests/test_calc.py"
TID = "T-261023-aaaaaa"


def _weaken(root):
    return {"file_path": str(root / T), "old_string": "    assert g()\n", "new_string": ""}


class TestWhyBlocks(TaskMixin, GatesCase):
    def setUp(self):
        super().setUp()
        self.write(T, "def test_a():\n    assert f()\n    assert g()\n")
        self.commit()
        self.hook("SessionStart", source="startup")
        self.set_task()
        p = mock.patch("vmc_harness.ledger.why.find_task", lambda root, i: self.task if i == self.task["task_id"] else None)
        p.start()
        self.addCleanup(p.stop)

    def _gate(self, res, gid="kernel:tamper"):
        return [c for c in res["candidates"] if c["kind"] == "gate" and c["id"] == gid]

    def test_block_event_carries_session_task_reason(self):
        self.assertDenied(self.hook("PreToolUse", "Edit", _weaken(self.root)))
        e = events.read(self.root, "gate_block")[-1]
        self.assertEqual(e["session_id"], self.session)
        self.assertEqual(e["task_id"], TID)
        self.assertTrue(e["reason"].startswith("검증물 변조 차단"))
        self.assertNotIn("[vmc-harness:", e["reason"])
        self.assertNotIn("\n", e["reason"])   # 사람에게 전하는 풀기 안내 줄은 빼고
        self.assertEqual(e["paths"], [T])
        self.assertEqual(len(events.read(self.root, "gate_block")), 1)   # 한 차단에 이벤트 하나

    def test_why_task_shows_reason(self):
        self.assertDenied(self.hook("PreToolUse", "Edit", _weaken(self.root)))
        g = self._gate(why.collect(self.root, TID))
        self.assertEqual(len(g), 1)
        self.assertIn("막음 1", g[0]["detail"])
        self.assertIn("최근 사유: 검증물 변조 차단", g[0]["detail"])
        self.assertIn("kernel:tamper", why.plain_text(why.collect(self.root, TID)))

    def test_proceed_counted_for_task(self):
        self.assertDenied(self.hook("PreToolUse", "Edit", _weaken(self.root)))
        self.hook("UserPromptSubmit", prompt="그냥 진행")
        e = events.read(self.root, "proceed_anyway")[-1]
        self.assertEqual((e["session_id"], e["task_id"]), (self.session, TID))
        self.assertIn("그냥 진행 1", self._gate(why.collect(self.root, TID))[0]["detail"])

    def test_other_task_block_not_attributed(self):
        self.set_task(task_id="T-261023-bbbbbb")
        self.assertDenied(self.hook("PreToolUse", "Edit", _weaken(self.root)))
        self.set_task()
        self.assertEqual(self._gate(why.collect(self.root, TID)), [])

    def test_why_file_finds_uncommitted_block(self):
        self.assertDenied(self.hook("PreToolUse", "Edit", _weaken(self.root)))
        g = self._gate(why.collect(self.root, T))
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0].get("via_task"), TID)
        self.assertIn("검증물 변조", g[0]["detail"])
        self.assertEqual(self._gate(why.collect(self.root, "tests/test_unrelated.py")), [])
