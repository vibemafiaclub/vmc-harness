"""착수 때 이 PC의 예측 치우침 한 줄 (spec 13 §조치 `predict` — 착수 때 넣는 보정 점수 표시).

현장 실측(합성으로 재현): 시간 실측/P50 중앙값 0.28(크게 잡음), 토큰 1.36(작게 잡음).
"""

import os
from unittest import mock

from tests.util import HarnessTestCase

from vmc_harness.core import config
from vmc_harness.core.project import pc_id
from vmc_harness.task import declare as dec, measure, state as st


def _rec(i, sec_ratio, tok_ratio, pc=None):
    return {"task_id": "T-%03d" % i, "pc_id": pc, "ended_at": "2026-11-%02dT10:00:00+09:00" % (1 + i % 28),
            "declare": {"intent": "합성 %d" % i, "scope": ["x/%d.py" % i],
                        "predict": {"agent_sec": {"p30": 300, "p50": 600, "p80": 1200},
                                    "tokens": {"p30": 50000, "p50": 100000, "p80": 200000}}},
            "actual": {"measured": True, "agent_sec": 600 * sec_ratio, "tokens": 100000 * tok_ratio}}


class PredictBiasTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        for k in st.SESSION_ENV:
            os.environ.pop(k, None)
        self.write("a.py", "x = 1\n")
        self.commit()
        st.session_touch(self.root, "S1", "claude")
        p = mock.patch("vmc_harness.task.pm.start", return_value="sync")
        p.start()
        self.addCleanup(p.stop)
        self.me = pc_id()

    def _rows(self, n, pc=None):
        return [_rec(i, 0.28, 1.36, pc or self.me) for i in range(n)]

    def test_bias_line(self):
        cal = measure.bias(self.root, self._rows(22))
        self.assertEqual(cal["n"], 22)
        self.assertAlmostEqual(cal["agent_sec"]["ratio_p50_median"], 0.28, places=2)
        line = measure.bias_text(self.root, cal)
        self.assertIn("최근 22건", line)
        self.assertIn("시간 0.28 — 크게 잡는 편", line)
        self.assertIn("토큰 1.36 — 작게 잡는 편", line)

    def test_needs_min_samples_from_this_pc(self):
        n = int(config.get("task.bias_min_n", self.root))
        self.assertIsNone(measure.bias(self.root, self._rows(n - 1)))
        self.assertIsNone(measure.bias(self.root, self._rows(n + 5, pc="other-pc")))
        self.assertIsNotNone(measure.bias(self.root, self._rows(n)))

    def test_window_keeps_recent(self):
        w = int(config.get("task.bias_window", self.root))
        old = [dict(_rec(i, 3.0, 1.0, self.me), ended_at="2026-01-01T00:00:00+09:00") for i in range(w)]
        cal = measure.bias(self.root, old + self._rows(w))
        self.assertEqual(cal["n"], w)
        self.assertAlmostEqual(cal["agent_sec"]["ratio_p50_median"], 0.28, places=2)

    def test_declare_context_shows_bias(self):
        raw = {"intent": "합성 작업", "scope": ["a.py"], "predict": {"tokens": [1, 2, 3], "agent_sec": [60, 120, 170]}}
        with mock.patch("vmc_harness.core.ledger.read_tasks", return_value=self._rows(22)):
            out = dec.declare(self.root, raw, session_id="S1")
        text = "\n".join(out["lines"])
        self.assertIn("[vmc-harness:declare_context]", text)
        self.assertIn("크게 잡는 편", text)
        with mock.patch("vmc_harness.core.ledger.read_tasks", return_value=self._rows(3)):
            out = dec.declare(self.root, dict(raw, intent="다른 합성 작업", scope=["b.py"]), session_id="S1",
                              mode="new")
        self.assertNotIn("치우침", "\n".join(out["lines"]))
