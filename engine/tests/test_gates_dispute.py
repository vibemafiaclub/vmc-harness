"""에이전트 반박(`proceed --dispute`) — 참고 지표만 (spec 02 §"그냥 진행"·01 P2).

현장 실측: 0.1.9 뒤 차단 안내문이 사람에게 전해졌지만 사람이 다음 요청으로 넘어가 헛막기 기록이 0건이었다.
에이전트가 근거를 댄 반박을 따로 세되, finding이 아니므로 통과·판정에는 쓰지 않는다.
"""

from tests.gates_util import GatesCase
from tests.test_gates_safety import TaskMixin
from vmc_harness.core import events, ledger
from vmc_harness.gates import proceed, state
from vmc_harness.ledger import metrics

EDIT = {"old_string": "assert 1", "new_string": "pass"}   # 검증 호출 감소 — 약화 신호


class TestDispute(TaskMixin, GatesCase):
    def setUp(self):
        super().setUp()
        self.write("tests/test_calc.py", "def test_a():\n    assert 1\n")
        self.commit()
        self.hook("SessionStart", source="startup")
        self.set_task()

    def _block(self):
        out = self.hook("PreToolUse", "Edit", dict(EDIT, file_path=str(self.root / "tests/test_calc.py")))
        self.assertDenied(out, "검증물 변조")
        return out

    def _events(self, kind):
        return events.read(str(self.root), kind)

    def test_hint_mentions_dispute(self):
        out = self._block()
        r = (out.get("hookSpecificOutput") or {}).get("permissionDecisionReason") or ""
        self.assertIn("--dispute", r)

    def test_dispute_records_metric_without_finding_or_bypass(self):
        self._block()
        b = proceed.dispute(self.root, self.session, "  기존 테스트에 줄만 더함  ")
        self.assertEqual(b["gate"], "tamper")
        # 차단 기록에 표시
        self.assertEqual(state.last_block(self.root, self.session)["disputed"], "기존 테스트에 줄만 더함")
        # finding 없음(01 P2)
        self.assertEqual([f for f in ledger.list_findings(self.root) if f.get("kind") == "false_block"], [])
        # 통과시키지 않는다
        self._block()
        # 이벤트 → 일일 집계의 disputed
        ev = self._events("block_disputed")
        self.assertEqual(len(ev), 1)
        day = list(metrics.aggregate(ev).values())[0]
        self.assertEqual(day["gates"][ev[0]["gate_id"]]["disputed"], 1)

    def test_dispute_twice_or_empty_rejected(self):
        self._block()
        with self.assertRaises(proceed.ProceedError):
            proceed.dispute(self.root, self.session, "   ")
        proceed.dispute(self.root, self.session, "근거")
        with self.assertRaises(proceed.ProceedError):
            proceed.dispute(self.root, self.session, "또")

    def test_human_proceed_still_works_after_dispute(self):
        self._block()
        proceed.dispute(self.root, self.session, "근거")
        self.hook("UserPromptSubmit", prompt="그냥 진행")
        self.assertEqual(len([f for f in ledger.list_findings(self.root) if f.get("kind") == "false_block"]), 1)
        self.assertAllowed(self.hook("PreToolUse", "Edit", dict(EDIT, file_path=str(self.root / "tests/test_calc.py"))))

    def test_dispute_targets_latest_block_even_if_proceeded(self):
        """사람이 방금 "그냥 진행"한 차단에 대한 반박이 그 앞의 다른 게이트 차단에 붙지 않는다(현장 결함 C11)."""
        self._block()
        st = state.load(self.root, self.session)
        st["blocks"].append({"gate": "overdesign", "gate_id": "kernel:overdesign", "at": "2999-01-01T00:00:00+00:00",
                             "fingerprint": "x", "event": "Stop", "reason": "r", "proceeded": True})
        state.save(self.root, self.session, st)
        with __import__("unittest").mock.patch("vmc_harness.gates.state.minutes_since", lambda iso: 0.0):
            b = proceed.dispute(self.root, self.session, "근거")
            self.assertEqual(b["gate"], "overdesign")
            b = proceed.dispute(self.root, self.session, "근거2", gate="tamper")
            self.assertEqual(b["gate"], "tamper")

    def test_not_allowed_gate_cannot_be_disputed(self):
        out = self.hook("PreToolUse", "Write", {"file_path": str(self.root / ".harness/ledger/x.json"), "content": "{}"})
        self.assertDenied(out)
        with self.assertRaises(proceed.ProceedError):
            proceed.dispute(self.root, self.session, "근거")
