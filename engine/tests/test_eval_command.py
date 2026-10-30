"""`vmc-harness eval` 명령 — 합성 데이터셋(렌더링 검사만·LLM 없음)·④ 소 작업 1건 실측."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ENGINE = Path(__file__).resolve().parent.parent


class EvalCommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vmh-evalcmd-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.ds = self.tmp / "ds"
        self.ds.mkdir()
        (self.ds / "render_cases.json").write_text(json.dumps({"cases": [
            {"id": "1", "facts": {"n": 3}, "draft": "시나리오 3건 통과"}]}, ensure_ascii=False), encoding="utf-8")

    def _run(self, *args):
        env = dict(os.environ, PYTHONPATH=str(ENGINE), VMC_HARNESS_HOME=str(self.tmp / "home"))
        return subprocess.run([sys.executable, "-m", "vmc_harness", "eval", "--dataset", str(self.ds)] + list(args),
                              cwd=str(self.tmp), env=env, capture_output=True, text=True, encoding="utf-8")

    def test_render_only_writes_result_and_baseline(self):
        p = self._run("--sets", "render", "--no-llm", "--set-baseline", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        out = json.loads(p.stdout)
        self.assertIsNone(out["compare"]["passed"])   # 첫 실행은 기준선 없음
        self.assertTrue((self.ds / "baseline.json").is_file())
        self.assertTrue((self.ds / "results" / "latest.json").is_file())
        p2 = self._run("--sets", "render", "--no-llm", "--json")
        self.assertEqual(p2.returncode, 0, p2.stderr)
        self.assertTrue(json.loads(p2.stdout)["compare"]["passed"])

    def test_unknown_set_is_input_error(self):
        self.assertEqual(self._run("--sets", "nope").returncode, 2)

    def test_missing_dataset_file_is_set_error_not_crash(self):
        p = self._run("--sets", "diagnosis", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        res = json.loads((self.ds / "results" / "latest.json").read_text(encoding="utf-8"))
        self.assertEqual(res["sets"]["diagnosis"]["status"], "error")


class OverheadTaskTest(unittest.TestCase):
    def test_small_task_measured(self):
        from vmc_harness.eval import overhead
        r = overhead.run_task("small", wait_s=0)
        self.assertEqual(r["size"], "small")
        self.assertGreater(r["sec"], 0)
        self.assertGreater(r["rule_block_tokens"], 0)
        self.assertIn("PreToolUse", r["per_event_ms"])


if __name__ == "__main__":
    unittest.main()
