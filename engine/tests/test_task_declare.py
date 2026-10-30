import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

from tests.util import HarnessTestCase

from vmc_harness.core import ledger
from vmc_harness.task import declare as dec, measure, size, state as st

ENGINE = str(Path(__file__).resolve().parent.parent)


def raw(**kw):
    d = {"intent": "목록 페이지 정렬 버그 수정", "scope": ["src/list.py"],
         "predict": {"tokens": [20000, 40000, 80000], "agent_sec": [60, 120, 300]}, "approvals_expected": 0}
    d.update(kw)
    return d


class DeclareTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        for k in st.SESSION_ENV:
            os.environ.pop(k, None)
        self.write("src/list.py", "x = 1\n")
        self.commit()

    def test_missing_fields_listed(self):
        with self.assertRaises(dec.DeclareError) as cm:
            dec.normalize({"intent": "", "predict": {"tokens": [3, 2, 1]}})
        msg = str(cm.exception)
        for part in ("intent", "scope", "토큰", "에이전트 작업 시간"):
            self.assertIn(part, msg)

    def test_test_change_needs_reason(self):
        with self.assertRaises(dec.DeclareError):
            dec.normalize(raw(test_change={"files": ["tests/test_a.py"]}))
        d = dec.normalize(raw(test_change={"files": ["tests/test_a.py"], "reason": "기대값이 스펙과 다름"}))
        self.assertEqual(d["test_change"]["files"], ["tests/test_a.py"])

    def test_declare_small_and_update(self):
        st.session_touch(self.root, "S1", "claude")
        r = dec.declare(self.root, raw())
        self.assertTrue(r["created"])
        self.assertEqual(r["size"], size.SMALL)
        self.assertRegex(r["task"]["task_id"], r"^T-\d{6}-[a-z2-7]{6}$")
        self.assertEqual(r["task"]["session_id"], "S1")
        self.assertEqual(r["lines"][0], "예상 2~5분")
        self.assertEqual(st.current(self.root, "S1")["task_id"], r["task"]["task_id"])
        with mock.patch("vmc_harness.task.pm.run_pm", return_value=None), \
                mock.patch("vmc_harness.task.pm.run_cards", return_value=[]), \
                mock.patch("vmc_harness.task.pm.is_sync", return_value=True):
            r2 = dec.declare(self.root, raw(scope=["src/"], predict={"tokens": [1, 2, 3], "agent_sec": [600, 900, 1200]}))
        self.assertFalse(r2["created"])
        self.assertEqual(r2["task"]["task_id"], r["task"]["task_id"])
        self.assertEqual(r2["size"], size.MID)
        self.assertEqual(r2["pm_mode"], "sync")
        self.assertEqual(r2["task"]["declare_rev"], 2)

    def test_session_from_env(self):
        os.environ["CLAUDE_CODE_SESSION_ID"] = "ENV1"
        r = dec.declare(self.root, raw())
        self.assertEqual(r["task"]["session_id"], "ENV1")

    def test_codex_env_wins_over_inherited_claude(self):
        # Claude 안에서 띄운 Codex: 바깥 Claude 값을 물려받아도 Codex 값으로 해석해야 한다
        os.environ["CLAUDE_CODE_SESSION_ID"] = "OUTER-CLAUDE"
        os.environ["CODEX_THREAD_ID"] = "CODEX-T1"
        self.assertEqual(st.resolve_session_id(self.root), "CODEX-T1")
        from vmc_harness.gates import state as gstate
        self.assertEqual(gstate.env_session_id(), "CODEX-T1")

    def test_size_rules(self):
        self.assertEqual(size.judge(dec.normalize(raw()), self.root), "small")
        self.assertEqual(size.judge(dec.normalize(raw(scope=["a.py", "b.py"])), self.root), "mid")
        self.assertEqual(size.judge(dec.normalize(raw(scope=["src/"])), self.root), "mid")
        self.assertEqual(size.judge(dec.normalize(raw(predict={"tokens": [1, 2, 3], "agent_sec": [60, 181, 200]})),
                                    self.root), "mid")

    def test_why_not_smaller(self):
        st.session_touch(self.root, "S1", "claude")
        dec.declare(self.root, raw())
        t = dec.why_not_smaller(self.root, "기존 관례를 따름")
        self.assertEqual(t["overdesign"]["why_not_smaller"], "기존 관례를 따름")

    def test_similar_context_only_at_declare(self):
        ledger.append_task(self.root, {"task_id": "T-old", "declare": dec.normalize(raw(intent="목록 페이지 정렬 수정")),
                                       "actual": {"measured": True, "agent_sec": 400, "tokens": 50000}})
        st.session_touch(self.root, "S1", "claude")
        r = dec.declare(self.root, raw())
        ctx = [l for l in r["lines"] if l.startswith("[vmc-harness:declare_context]")]
        self.assertEqual(len(ctx), 1)
        self.assertIn("에이전트 7분", ctx[0])

    def test_cli_stdin_and_args(self):
        env = dict(os.environ, PYTHONPATH=ENGINE, VMC_HARNESS_SESSION_ID="CLI1")
        p = subprocess.run([sys.executable, "-m", "vmc_harness", "declare"], input=json.dumps(raw()),
                           cwd=str(self.root), env=env, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("예상 2~5분", p.stdout)
        p = subprocess.run([sys.executable, "-m", "vmc_harness", "declare", "--intent", "x"],
                           cwd=str(self.root), env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, 2)
        self.assertIn("착수 선언에 빠진 것", p.stderr)
        p = subprocess.run([sys.executable, "-m", "vmc_harness", "declare", "--intent", "y", "--scope", "src/list.py",
                            "--tokens", "1,2,3", "--agent-min", "1,2,3"],
                           cwd=str(self.root), env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("선언 갱신", p.stdout)


class MeasureTest(HarnessTestCase):
    def test_calibration(self):
        pred = {"agent_sec": {"p30": 10, "p50": 20, "p80": 30}, "tokens": {"p30": 1, "p50": 2, "p80": 3}}
        tasks = [{"declare": {"predict": pred}, "actual": {"measured": True, "agent_sec": s, "tokens": 2}}
                 for s in (5, 15, 25, 40)]
        tasks.append({"declare": {"predict": pred}, "actual": {"measured": False, "agent_sec": None}})
        c = measure.calibration(tasks)
        self.assertEqual(c["agent_sec"]["n"], 4)
        self.assertEqual(c["agent_sec"]["p30"]["hit"], 0.25)
        self.assertEqual(c["agent_sec"]["p80"]["hit"], 0.75)
        self.assertEqual(c["agent_sec"]["over_p80"], 1)

    def test_estimate_line(self):
        self.assertEqual(measure.estimate_line({"predict": {"agent_sec": {"p50": 470, "p80": 900}}}), "예상 8~15분")
