import subprocess

from tests.util import HarnessTestCase

from vmc_harness.core import ledger
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.ledger import git_watch, revert


def _git(root, *a):
    return subprocess.run(["git"] + list(a), cwd=root, capture_output=True, text=True)


class RevertTest(HarnessTestCase):
    def _action_commit(self):
        self.write("a.txt", "1\n")
        self.commit("base")
        a = ledger.create_action(self.root, {"type": "machine_fact", "command": "cat x"}, "gate", "h",
                                 target={"gate_id": "g1"})
        self.write("a.txt", "2\n")
        self.commit("gate\n\nHarness-Action: %s\nHarness-Task: T-261022-aaaaaa" % a["action_id"])
        return a

    def test_human_git_revert_detected_once(self):
        a = self._action_commit()
        _git(self.root, "revert", "--no-edit", "HEAD")
        fs = git_watch.scan(self.root)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0]["kind"], "revert")
        self.assertEqual(fs[0]["target"]["action_id"], a["action_id"])
        self.assertEqual(git_watch.scan(self.root), [])

    def test_harness_revert_not_detected(self):
        a = self._action_commit()
        revert.revert_action(self.root, a["action_id"], "test", by_human=False)
        self.assertEqual(git_watch.scan(self.root), [])
        self.assertEqual(ledger.get_action(self.root, a["action_id"])["status"], "reverted")

    def test_cli_revert_action_makes_finding(self):
        a = self._action_commit()
        revert.revert(self.root, a["action_id"])
        self.assertEqual(ledger.list_findings(self.root)[0]["kind"], "revert")
        self.assertEqual((self.root / "a.txt").read_text(), "1\n")

    def test_revert_task(self):
        a = self._action_commit()
        res = revert.revert_task(self.root, "T-261022-aaaaaa", "사람 요청")
        self.assertEqual(res["actions"], [a["action_id"]])
        self.assertEqual(ledger.get_action(self.root, a["action_id"])["status"], "reverted")
        self.assertEqual((self.root / "a.txt").read_text(), "1\n")

    def test_staged_changes_defer(self):
        a = self._action_commit()
        self.write("b.txt", "x")
        _git(self.root, "add", "b.txt")
        rec = revert.revert_action(self.root, a["action_id"], "r", by_human=False)
        self.assertIn("deferred", rec)
        self.assertEqual(ledger.get_action(self.root, a["action_id"])["status"], "trial")
        self.commit("other")
        self.assertEqual(revert.retry_pending(self.root), [a["action_id"]])

    def test_uncommitted_rule_unapply(self):
        write_json(self.root / ".harness" / "rules.json", {"rules": [{"id": "R-1", "text": "앞으로 X 해"}]})
        a = ledger.create_action(self.root, {"type": "explicit_request", "quote": "앞으로 X 해"}, "rule", "h",
                                 why_not_higher="기계 판정 불가", human_requested=True, target={"rule_id": "R-1"})
        revert.revert_action(self.root, a["action_id"], "r")
        self.assertEqual(read_json(self.root / ".harness" / "rules.json")["rules"][0]["status"], "reverted")

    def test_human_revert_restores_ledger_records(self):
        a = self._action_commit()  # 조치 기록 파일이 같은 커밋에 실림
        _git(self.root, "revert", "--no-edit", "HEAD")
        self.assertIsNone(ledger.get_action(self.root, a["action_id"]))
        git_watch.scan(self.root)
        self.assertEqual(ledger.get_action(self.root, a["action_id"])["action_id"], a["action_id"])
        from vmc_harness.ledger import judge
        judge.run(self.root)
        self.assertEqual(ledger.get_action(self.root, a["action_id"])["status"], "reverted")


class ExplicitRuleRevertTest(HarnessTestCase):
    def test_diagnose_rule_is_revertible_by_rule_id(self):
        """진단 [추가] 규칙도 자가 점검과 같은 경로(target rule_id) — 원복하면 규칙 블록에서 빠진다."""
        from vmc_harness.bootstrap import diagnose
        from vmc_harness.gates import rules
        r = diagnose._add_rule(self.root, {"blocks": "같은 지시 반복 감소", "scope": "be/",
                                           "rule_text": "앞으로 SQL은 SQL/ 아래에만 둔다"}, "앞으로 SQL은 SQL/ 아래에만")
        a = ledger.get_action(self.root, r["action_id"])
        self.assertEqual(a["target"], {"rule_id": r["rule_id"]})
        self.assertIn("앞으로 SQL은", rules.render_block(self.root))
        self.assertIn("앞으로 SQL은", (self.root / "AGENTS.md").read_text(encoding="utf-8"))
        revert.revert_action(self.root, r["action_id"], "테스트")
        self.assertNotIn("앞으로 SQL은", rules.render_block(self.root))
        self.assertNotIn("앞으로 SQL은", (self.root / "AGENTS.md").read_text(encoding="utf-8"))
