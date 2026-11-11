"""정기 재진단: 지난 진단 뒤 입력(새 대화·새 작업)이 거의 없으면 다시 만들지 않는다(skipped_no_change). 합성 데이터."""

import datetime as _dt
from unittest import mock

from tests.util import HarnessTestCase
from vmc_harness.bootstrap import diagnose, sessions_input, weekly
from vmc_harness.core import events, ledger
from vmc_harness.core.clock import now
from vmc_harness.core.jsonio import read_json, write_json


def _sess(sid, at):
    return {"session_id": sid, "agent": "claude", "kind": "main", "started_at": at, "ended_at": at,
            "human_prompts": [{"at": at, "text": "x"}], "tool_calls": [], "human_turns": 1}


class WeeklySkipTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.write("a.py", "x = 1\n")
        self.commit("base")
        self.prev_at = now() - _dt.timedelta(days=1)
        write_json(diagnose.diagnosis_path(self.root), {"status": "ok", "proposals": [],
                                                        "generated_at": self.prev_at.isoformat(timespec="seconds")})

    def _job(self, sessions):
        with mock.patch.object(sessions_input, "load", return_value=(sessions, {"main": len(sessions)})), \
                mock.patch.object(diagnose, "run") as run, \
                mock.patch.object(weekly, "refresh_baselines") as rb:
            st = weekly.job(self.root, pdf=False)
        return st, run, rb

    def test_little_change_skips_without_model(self):
        old = (self.prev_at - _dt.timedelta(days=3)).isoformat()
        new = now().isoformat()
        sess = [_sess("old%d" % i, old) for i in range(10)] + [_sess("new%d" % i, new) for i in range(4)]
        for i in range(4):
            ledger.append_task(self.root, {"task_id": "T-%d" % i, "started_at": new})
        ledger.append_task(self.root, {"task_id": "T-old", "started_at": old})
        st, run, rb = self._job(sess)
        run.assert_not_called()
        rb.assert_not_called()
        self.assertEqual(st["status"], "skipped_no_change")
        self.assertEqual((st["new_sessions"], st["new_tasks"]), (4, 4))
        self.assertEqual(read_json(weekly.state_path(self.root))["status"], "skipped_no_change")
        ev = events.read(self.root, "diagnose_weekly")
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["status"], ev[0]["tokens"]), ("skipped_no_change", 0))
        # 같은 기간엔 다시 띄우지 않는다
        with mock.patch("vmc_harness.core.llm.available", return_value=True), \
                mock.patch("vmc_harness.core.llm.nested", return_value=False):
            self.assertEqual(weekly.due(self.root), (False, "이번 기간 이미 skipped_no_change"))

    def test_enough_new_sessions_or_tasks_runs(self):
        new = now().isoformat()
        for sessions, tasks in ((5, 0), (0, 5)):
            for i in range(tasks):
                ledger.append_task(self.root, {"task_id": "T-n%d" % i, "started_at": new})
            d = {"status": "ok", "proposals": [], "tokens": 1, "sec": 1}
            with mock.patch.object(sessions_input, "load",
                                   return_value=([_sess("n%d" % i, new) for i in range(sessions)], {"main": sessions})), \
                    mock.patch.object(diagnose, "run", return_value=d) as run, \
                    mock.patch.object(weekly, "refresh_baselines", return_value={}):
                st = weekly.job(self.root, pdf=False)
            run.assert_called_once()
            self.assertEqual(st["status"], "ok")

    def test_unknown_previous_time_runs(self):
        self.assertEqual(weekly.changes_since(self.root, [], None)["sessions"], float("inf"))
