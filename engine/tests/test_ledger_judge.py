import datetime as _dt
import subprocess

from tests.util import HarnessTestCase

from vmc_harness.core import ledger, notices
from vmc_harness.core.jsonio import read_json
from vmc_harness.core.project import pc_id
from vmc_harness.ledger import exposure, judge, metrics


def _gate_action(root, gid="g1", human=False):
    basis = {"type": "explicit_request", "quote": "앞으로 이건 막아"} if human else {"type": "machine_fact", "command": "cat x"}
    return ledger.create_action(root, basis, "gate", "막으면 재작업이 준다", human_requested=human,
                                target={"gate_id": gid})


def _after_min_days(root):
    from vmc_harness.core import config
    return _dt.datetime.now().astimezone() + _dt.timedelta(days=config.get("judge.min_days", root), hours=1)


class JudgeTest(HarnessTestCase):
    def test_kept_after_exposures(self):
        a = _gate_action(self.root)
        for _ in range(10):
            exposure.record(self.root, gate_id="g1")
        self.assertEqual(judge.run(self.root), [])  # 노출을 하루에 채워도 최소 기간 전엔 존속 아님
        vs = judge.run(self.root, at=_after_min_days(self.root))
        self.assertEqual([v["result"] for v in vs], ["kept"])
        self.assertEqual(ledger.get_action(self.root, a["action_id"])["status"], "kept")
        self.assertEqual(ledger.list_verdicts(self.root)[0]["exposures"], 10)
        m = read_json(ledger.metrics_path(self.root, metrics.today(), pc_id()))
        self.assertEqual(m["exposures"][a["action_id"]], 10)

    def test_team_exposures_sum_across_pcs(self):
        from vmc_harness.core.jsonio import write_json
        a = _gate_action(self.root)
        for _ in range(4):
            exposure.record(self.root, gate_id="g1")
        write_json(ledger.metrics_path(self.root, metrics.today(), "other-pc"),
                   {"pc_id": "other-pc", "date": metrics.today(), "exposures": {a["action_id"]: 6}})
        self.assertEqual(judge.run(self.root, at=_after_min_days(self.root))[0]["result"], "kept")

    def test_trial_continues_and_progress(self):
        a = _gate_action(self.root)
        for _ in range(3):
            exposure.record(self.root, gate_id="g1")
        self.assertEqual(judge.run(self.root), [])
        p = judge.progress(self.root)
        self.assertEqual(p[0]["exposures"], 3)
        self.assertEqual(ledger.get_action(self.root, a["action_id"])["status"], "trial")

    def test_false_block_threshold(self):
        a = _gate_action(self.root)
        for _ in range(3):
            ledger.create_finding(self.root, "false_block", {"gate_id": "g1"})
        judge.run(self.root)
        self.assertEqual(ledger.get_action(self.root, a["action_id"])["status"], "trial")
        ledger.create_finding(self.root, "false_block", {"gate_id": "g1"})
        judge.run(self.root)
        self.assertEqual(ledger.get_action(self.root, a["action_id"])["status"], "reverted")
        self.assertTrue(any(n["kind"] == "action_reverted" for n in notices.pending(self.root)))

    def test_false_block_at_full_exposure_reverts(self):
        a = _gate_action(self.root)
        ledger.create_finding(self.root, "false_block", {"gate_id": "g1"})
        for _ in range(10):
            exposure.record(self.root, gate_id="g1")
        judge.run(self.root)
        self.assertEqual(ledger.get_action(self.root, a["action_id"])["status"], "reverted")

    def test_blame_immediate_with_git_revert(self):
        self.write("a.txt", "1\n")
        self.commit("base")
        a = _gate_action(self.root)
        self.write("a.txt", "2\n")
        self.commit("gate change\n\nHarness-Action: %s" % a["action_id"])
        ledger.create_finding(self.root, "blame", {"action_id": a["action_id"]})
        vs = judge.run(self.root)
        self.assertEqual(vs[0]["result"], "reverted")
        self.assertEqual((self.root / "a.txt").read_text(), "1\n")
        log = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=self.root, capture_output=True, text=True).stdout
        self.assertIn("Harness-Reverted-By: harness", log)
        self.assertIn("Harness-Action: %s" % a["action_id"], log)

    def test_deadline_not_enough_exposure(self):
        old = (_dt.datetime.now().astimezone() - _dt.timedelta(days=30)).isoformat(timespec="seconds")
        a = _gate_action(self.root, "g1")
        ledger.update_action(self.root, a["action_id"], created_at=old)
        h = _gate_action(self.root, "g2", human=True)
        ledger.update_action(self.root, h["action_id"], created_at=old)
        judge.run(self.root)
        self.assertEqual(ledger.get_action(self.root, a["action_id"])["status"], "reverted")
        hh = ledger.get_action(self.root, h["action_id"])
        self.assertEqual(hh["status"], "extended")
        self.assertTrue(hh["trial"]["deadline"])

    def test_findings_before_action_ignored(self):
        a = _gate_action(self.root)
        f = {"kind": "blame", "target": {"gate_id": "g1"}, "at": "2000-01-01T00:00:00+00:00"}
        self.assertEqual(judge.negatives(a, [f]), (0, 0))
        self.assertEqual(judge.negatives(a, [dict(f, at=a["created_at"])]), (1, 0))

    def test_metrics_aggregate_and_team(self):
        from vmc_harness.core import events
        events.log(self.root, "gate_fire", gate_id="g9")
        events.log(self.root, "gate_block", gate_id="g9")
        events.log(self.root, "proceed_anyway", gate_id="g9")
        events.log(self.root, "gate_time", gate_id="g9", sec=1.5)
        events.log(self.root, "session_selfcheck", measured=True, human_turns=4, tool_calls=40, corrections=2)
        events.log(self.root, "card_search", ids=["K-1"], sec=0.2, tokens=10)
        events.log(self.root, "exposure", action_ids=[], card="K-1")
        metrics.rollup(self.root)
        t = metrics.team(self.root)
        self.assertEqual(t["gates"]["g9"], {"fired": 1, "blocked": 1, "false_block": 1, "disputed": 0, "sec": 1.5})
        self.assertEqual(t["cards"]["K-1"], 1)
        self.assertEqual(t["reference"]["corrections_per_100_tool_calls"], 5.0)
        self.assertEqual(t["reference"]["human_turns_per_session"], 4.0)
        self.assertEqual(metrics.rollup(self.root), [])  # 다시 돌려도 같은 결과면 안 쓴다

    def test_due_interval(self):
        self.assertTrue(judge.due(self.root))
        judge.mark_run(self.root)
        self.assertFalse(judge.due(self.root))
