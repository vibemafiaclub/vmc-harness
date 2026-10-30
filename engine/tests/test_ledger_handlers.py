import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

from tests.util import HarnessTestCase

from vmc_harness.core import events, ledger
from vmc_harness.core.jsonio import write_json
from vmc_harness.hooks.contract import HookContext
from vmc_harness.ledger import handlers, judge


class HandlersTest(HarnessTestCase):
    def test_session_start_spawns_once_a_day(self):
        with mock.patch.object(handlers, "spawn_cli") as sp:
            handlers.on_session_start(HookContext("SessionStart", "claude", {}, root=str(self.root), session_id="S1"))
            handlers.on_session_start(HookContext("SessionStart", "claude", {}, root=str(self.root), session_id="S2"))
        self.assertEqual(sp.call_count, 1)
        self.assertEqual(sp.call_args[0][0][:2], ["judge", "--background"])

    def test_nested_does_nothing(self):
        with mock.patch.dict(os.environ, {"VMC_HARNESS_NESTED": "1"}), mock.patch.object(handlers, "spawn_cli") as sp:
            handlers.on_session_start(HookContext("SessionStart", "claude", {}, root=str(self.root)))
        sp.assert_not_called()

    def test_rule_exposure_once_per_session(self):
        write_json(self.root / ".harness" / "rules.json", {"rules": [{"id": "R-1", "text": "앞으로 X"}]})
        a = ledger.create_action(self.root, {"type": "explicit_request", "quote": "앞으로 X"}, "rule", "h",
                                 why_not_higher="w", human_requested=True, target={"rule_id": "R-1"})
        judge.mark_run(self.root)
        for _ in range(2):
            handlers.on_session_start(HookContext("SessionStart", "claude", {}, root=str(self.root), session_id="S1"))
        exp = [e for e in events.read(self.root, "exposure") if a["action_id"] in e["action_ids"]]
        self.assertEqual(len(exp), 1)

    def test_area_exposure_and_session_end(self):
        a = ledger.create_action(self.root, {"type": "cleansing"}, "code", "정리", target={"area": "be/mod"})
        ctx = HookContext("PostToolUse", "claude", {}, root=str(self.root), session_id="S1", tool_name="Edit",
                          write_paths=["be/mod/x.py"])
        handlers.on_post_tool(ctx)
        handlers.on_post_tool(ctx)
        exp = [e for e in events.read(self.root, "exposure") if a["action_id"] in e["action_ids"]]
        self.assertEqual(len(exp), 1)
        with mock.patch.object(handlers, "spawn_cli") as sp:
            end = HookContext("SessionEnd", "claude", {}, root=str(self.root), session_id="S1", transcript_path="/t/S1.jsonl")
            handlers.on_session_end(end)
            handlers.on_session_end(end)
        self.assertEqual(sp.call_count, 1)
        self.assertIn("selfcheck", sp.call_args[0][0])

    def test_background_job(self):
        out = handlers.background_job(self.root)
        self.assertIn("verdicts", out)
        self.assertFalse(judge.due(self.root))


class CliTest(HarnessTestCase):
    def _cli(self, *args):
        engine = str(Path(__file__).resolve().parent.parent)
        env = dict(os.environ, PYTHONPATH=engine)
        return subprocess.run([sys.executable, "-m", "vmc_harness"] + list(args), cwd=str(self.root), env=env,
                              capture_output=True, text=True, timeout=60)

    def test_judge_why_finding_card(self):
        self.write("a.txt", "1")
        self.commit("base")
        r = self._cli("judge")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._cli("judge", "--status").returncode, 0)
        r = self._cli("why", "a.txt", "--json")
        self.assertEqual(json.loads(r.stdout)["target"], "a.txt")
        r = self._cli("card", "add", "--title", "한 페이지 먼저", "--judgment", "한 페이지로 확인", "--quote",
                      "앞으로 한 페이지 먼저 해", "--why-not-higher", "취향이라 기계 판정 불가")
        self.assertEqual(r.returncode, 0, r.stderr)
        cid = json.loads(r.stdout)["card"]
        r = self._cli("finding", "--blame", cid)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(ledger.list_findings(self.root)[0]["kind"], "blame")
        r = self._cli("card", "add", "--title", "t", "--judgment", "j", "--quote", "q", "--why-not-higher", "x" * 201)
        self.assertEqual(r.returncode, 1)
        self.assertIn("200자", r.stderr)
