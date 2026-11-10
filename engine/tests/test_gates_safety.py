"""보호 영역·검증물 변조·완료 게이트·그냥 진행 (spec 02 안전 게이트)."""

import json
import os
import sys
import time
import unittest
from unittest import mock

from tests.gates_util import GatesCase
from vmc_harness.core import ledger, notices
from vmc_harness.core.clock import now_iso
from vmc_harness.core.jsonio import write_json
from vmc_harness.gates import evidence, state


class TaskMixin(object):
    task = None

    def set_task(self, **kw):
        t = {"task_id": "T-261023-aaaaaa", "session_id": self.session, "size": "mid",
             "started_at": "2000-01-01T00:00:00+00:00", "declare": {"scope": [], "protected_touch": False, "test_change": None}}
        t["declare"].update(kw.pop("declare", {}))
        t.update(kw)
        self.task = t
        p = mock.patch("vmc_harness.gates.state.current_task", lambda root, sid: self.task)
        p.start()
        self.addCleanup(p.stop)
        return t

    def write(self, rel, text):
        """작업이 있으면 에이전트 편집으로 — 훅이 남기는 기록(작업 편집·세션 쓰기)을 같이 남긴다."""
        p = super().write(rel, text)
        if self.task is not None:
            from vmc_harness.task import scope
            scope.record_writes(self.task, [rel])
            st = state.load(self.root, self.session)
            w = st.setdefault("writes", [])
            if rel not in w:
                w.append(rel)
            state.save(self.root, self.session, st)
        return p


class TestProtected(TaskMixin, GatesCase):
    def setUp(self):
        super().setUp()
        write_json(self.root / ".harness/protected.json",
                   {"paths": ["be/auth/**", {"glob": "be/work_requests/**", "kind": "approval_flow"}]})
        self.write("be/auth/reauth.py", "x = 1\n")
        self.commit()

    def edit(self, rel):
        return self.hook("PreToolUse", "Edit", {"file_path": str(self.root / rel), "old_string": "x", "new_string": "y"})

    def test_undeclared_blocks_with_one_card(self):
        self.set_task()
        self.assertDenied(self.edit("be/auth/reauth.py"), "보호 영역 수정 승인 필요")
        aps = ledger.list_approvals(self.root)
        self.assertEqual(len(aps), 1)
        self.assertEqual(aps[0]["criterion"], 2)
        # 다시 시도해도 새 카드 없이 승인 대기
        self.assertDenied(self.edit("be/auth/reauth.py"), "승인 대기")
        self.assertEqual(len(ledger.list_approvals(self.root)), 1)
        ledger.answer_approval(self.root, aps[0]["approval_id"], "approve")
        self.assertAllowed(self.edit("be/auth/reauth.py"))

    def test_declared_asks_once_at_start(self):
        self.set_task(declare={"protected_touch": True, "scope": ["be/auth/"]})
        self.hook("PostToolUse", "Bash", {"command": "vmc-harness declare --protected-touch"})
        aps = ledger.list_approvals(self.root)
        self.assertEqual(len(aps), 1)
        self.assertTrue(aps[0]["detail"]["declared"])
        self.assertIn("be/auth/**", aps[0]["detail"]["globs"])
        self.hook("PostToolUse", "Bash", {"command": "ls"})
        self.assertEqual(len(ledger.list_approvals(self.root)), 1)
        ledger.answer_approval(self.root, aps[0]["approval_id"], "approve")
        self.assertAllowed(self.edit("be/auth/reauth.py"))

    def test_rejected_and_shell_write(self):
        self.set_task()
        self.edit("be/auth/reauth.py")
        a = ledger.list_approvals(self.root)[0]
        ledger.answer_approval(self.root, a["approval_id"], "reject")
        self.assertDenied(self.edit("be/auth/reauth.py"), "거절")
        out = self.hook("PreToolUse", "Bash", {"command": "echo y > be/auth/reauth.py"})
        self.assertDenied(out)

    def test_no_task_blocks_without_card(self):
        self.set_task()
        self.task = None
        self.assertDenied(self.edit("be/auth/reauth.py"), "보호 영역 수정 차단")
        self.assertEqual(ledger.list_approvals(self.root), [])


class TestTamper(TaskMixin, GatesCase):
    def setUp(self):
        super().setUp()
        self.write("tests/test_calc.py", "def test_a():\n    assert 1\n")
        self.write(".harness/qa/scenarios/s1.json", "{}")
        self.write("src/calc.py", "x=1\n")
        self.commit()

    def test_existing_test_edit_blocked_new_allowed(self):
        self.set_task()
        out = self.hook("PreToolUse", "Edit", {"file_path": str(self.root / "tests/test_calc.py"), "old_string": "1", "new_string": "0"})
        self.assertDenied(out, "검증물 변조")
        out = self.hook("PreToolUse", "Write", {"file_path": str(self.root / "tests/test_new.py"), "content": "x"})
        self.assertAllowed(out)
        out = self.hook("PreToolUse", "Write", {"file_path": str(self.root / ".harness/qa/scenarios/s1.json"), "content": "{}"})
        self.assertDenied(out)

    def test_shell_and_patch(self):
        self.set_task()
        for cmd in ("rm -rf tests", "sed -i 's/1/0/' tests/test_calc.py", "git checkout -- tests/test_calc.py"):
            self.assertDenied(self.hook("PreToolUse", "Bash", {"command": cmd}), "검증물 변조")
        # 해석 못 한 명령은 미리 짐작하지 않고 실행 뒤 대조로 되돌린다(test_gates_opaque.TestOpaqueTamper)
        cmd = "python3 -c \"open('tests/test_calc.py','w')\""
        p = self.root / "tests/test_calc.py"
        before = p.read_bytes()
        self.assertAllowed(self.hook("PreToolUse", "Bash", {"command": cmd}, tool_use_id="t1"))
        p.write_text("", encoding="utf-8")
        post = self.hook("PostToolUse", "Bash", {"command": cmd}, tool_use_id="t1")
        self.assertEqual(post.get("decision"), "block", post)
        self.assertEqual(p.read_bytes(), before)
        patch = "*** Begin Patch\n*** Delete File: tests/test_calc.py\n*** End Patch"
        self.assertDenied(self.hook("PreToolUse", "apply_patch", {"command": patch}, agent="codex"))

    def test_declared_test_change_allowed(self):
        self.set_task(declare={"test_change": {"files": ["tests/test_calc.py"], "reason": "기대값이 요구사항과 다름"}})
        out = self.hook("PreToolUse", "Edit", {"file_path": str(self.root / "tests/test_calc.py"), "old_string": "1", "new_string": "0"})
        self.assertAllowed(out)
        self.set_task(declare={"test_change": {"files": ["tests/test_calc.py"], "reason": ""}})
        out = self.hook("PreToolUse", "Edit", {"file_path": str(self.root / "tests/test_calc.py"), "old_string": "1", "new_string": "0"})
        self.assertDenied(out)


class TestCompletion(TaskMixin, GatesCase):
    def setUp(self):
        super().setUp()
        self.write("fe/pages/settle.tsx", "export const A = 1\n")
        self.write("lib/util.py", "x = 1\n")
        self.write("tests/test_x.py", "def test(): pass\n")
        self.commit()
        self.hook("SessionStart", source="startup")

    def stop(self):
        return self.hook("Stop", stop_hook_active=False)

    def test_no_behavior_change_passes(self):
        self.set_task(size="small")
        self.write("lib/util.py", "x = 2\n")
        self.assertEqual(self.stop(), {})

    def test_page_change_needs_evidence_after_edit(self):
        t = self.set_task()
        self.write("fe/pages/settle.tsx", "export const A = 2\n")
        out = self.stop()
        self.assertEqual(out.get("decision"), "block")
        self.assertIn("[vmc-harness:missing_evidence]", out["reason"])
        self.assertIn("fe/pages/settle.tsx", out["reason"])
        png = self._tmp / "shot.png"
        png.write_bytes(b"\x89PNG....")
        evidence.screenshot(self.root, t["task_id"], str(png), "정산 페이지")
        self.assertEqual(self.stop(), {})
        time.sleep(0.05)
        self.write("fe/pages/settle.tsx", "export const A = 3\n")   # 증거 뒤 다시 편집
        self.assertEqual(self.stop().get("decision"), "block")

    def test_old_screenshot_not_accepted(self):
        t = self.set_task()
        png = self._tmp / "old.png"
        png.write_bytes(b"\x89PNG")
        os.utime(str(png), (time.time() - 3600, time.time() - 3600))
        self.write("fe/pages/settle.tsx", "export const A = 2\n")
        evidence.screenshot(self.root, t["task_id"], str(png), "정산")
        self.assertEqual(self.stop().get("decision"), "block")

    def test_test_run_new_failures_not_accepted(self):
        t = self.set_task()
        self.write("fe/pages/settle.tsx", "export const A = 2\n")
        with mock.patch("vmc_harness.gates.evidence.baseline_failures", lambda root: ["tests/test_old.py::test_a"]):
            m = evidence.test_run(self.root, t["task_id"], [sys.executable, "-c", "print('FAILED tests/test_new.py::test_b'); raise SystemExit(1)"])
            self.assertFalse(evidence.marker_ok(m))
            out = self.stop()
            self.assertIn("새 실패", out["reason"])
            m = evidence.test_run(self.root, t["task_id"], [sys.executable, "-c", "print('FAILED tests/test_old.py::test_a'); raise SystemExit(1)"])
            self.assertTrue(evidence.marker_ok(m))
        self.assertEqual(self.stop(), {})

    def test_approval_flow_requires_qa_scenario(self):
        write_json(self.root / ".harness/protected.json", {"paths": [{"glob": "be/work_requests/**", "kind": "approval_flow"}]})
        self.write("be/work_requests/repo.py", "y=1\n")
        self.commit("wr")
        st = state.load(self.root, self.session)
        st["base_head"] = None
        state.save(self.root, self.session, st)
        t = self.set_task()
        self.write("be/work_requests/repo.py", "y=2\n")
        evidence.make(self.root, t["task_id"], "db_dryrun", "SQL/x.sql", data={"affected_rows": 1})
        out = self.stop()
        self.assertIn("QA 시나리오", out["reason"])
        evidence.make(self.root, t["task_id"], "qa_scenario", "QA-001", data={"status": "PASS"})
        self.assertEqual(self.stop(), {})

    def test_mid_task_impact_reaches_page(self):
        self.write("fe/lib/fmt.ts", "export const f = 1\n")
        self.write("fe/pages/list.tsx", "import { f } from '../lib/fmt'\nexport default f\n")
        self.commit("ts")
        st = state.load(self.root, self.session)
        st["base_head"] = None
        state.save(self.root, self.session, st)
        self.set_task(size="mid")
        self.write("fe/lib/fmt.ts", "export const f = 2\n")
        out = self.stop()
        self.assertEqual(out.get("decision"), "block")
        self.set_task(size="small")
        self.assertEqual(self.stop(), {})

    def test_max_blocks_then_pass_with_notice(self):
        self.set_task()
        self.write("fe/pages/settle.tsx", "export const A = 2\n")
        from vmc_harness.core import config
        n = config.get("gates.completion_max_blocks")
        for _ in range(n):
            self.assertEqual(self.stop().get("decision"), "block")
        self.assertEqual(self.stop(), {})
        kinds = [x["kind"] for x in notices.pending(self.root)]
        self.assertIn("gate_blocked", kinds)

    def test_proceed_by_prompt_records_false_block(self):
        self.set_task()
        self.write("fe/pages/settle.tsx", "export const A = 2\n")
        self.assertEqual(self.stop().get("decision"), "block")
        self.hook("UserPromptSubmit", prompt="이건 화면 영향 없어, 그냥 진행해")
        fs = ledger.list_findings(self.root)
        self.assertEqual([f["kind"] for f in fs], ["false_block"])
        self.assertEqual(fs[0]["target"]["gate_id"], "kernel:completion")
        self.assertEqual(self.stop(), {})            # 1회 통과
        self.assertEqual(self.stop().get("decision"), "block")

    def test_proceed_not_for_engine_guard_and_cli_needs_tty(self):
        from vmc_harness.gates import proceed
        self.hook("PreToolUse", "Write", {"file_path": str(self.root / ".harness/ledger/x.json"), "content": "{}"})
        self.hook("UserPromptSubmit", prompt="그냥 진행")
        self.assertEqual(ledger.list_findings(self.root), [])
        with self.assertRaises(proceed.ProceedError):
            proceed.from_cli(self.root)            # 테스트 stdin은 터미널이 아님


if __name__ == "__main__":
    unittest.main()
