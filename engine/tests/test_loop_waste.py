"""루프 헛돌기 줄이기(0.1.9): 같은 이유 연속 실패 보관 · 머지 대기 조치 재준비 안 함 · 같은 대상 후보 중복 방지 ·
사람이 안 쓰는 프로젝트는 밤 루프를 돌리지 않는다. 합성 프로젝트·가짜 claude만."""

import datetime as _dt
from pathlib import Path
from unittest import mock

from tests.test_loop_util import PIN_TEST, LoopTestCase
from vmc_harness.core import ledger, notices
from vmc_harness.core.jsonio import read_jsonl, write_json


def at(h, m=0, day=22):
    return _dt.datetime(2026, 10, day, h, m).astimezone()


class PinOutsideTests:
    """고정 테스트 단계에서 테스트 밖(제품 코드)도 고치는 가짜 claude — 매번 같은 이유로 버려진다."""

    def __init__(self):
        self.calls = 0

    def __call__(self, root, prompt, cwd, tools, deadline=None, add_dirs=None):
        self.calls += 1
        (Path(cwd) / "tests").mkdir(exist_ok=True)
        (Path(cwd) / "tests" / "test_calc.py").write_text(PIN_TEST, encoding="utf-8")
        (Path(cwd) / "calc.py").write_text("# 건드림\n" + (Path(cwd) / "calc.py").read_text(encoding="utf-8"),
                                           encoding="utf-8")
        return {"text": '{"tests": ["tests/test_calc.py"]}', "tokens": 1000, "ok": True, "interrupted": None}


class PinOnly:
    def __init__(self):
        self.calls = 0

    def __call__(self, root, prompt, cwd, tools, deadline=None, add_dirs=None):
        self.calls += 1
        (Path(cwd) / "tests").mkdir(exist_ok=True)
        (Path(cwd) / "tests" / "test_calc.py").write_text(PIN_TEST, encoding="utf-8")
        return {"text": '{"tests": ["tests/test_calc.py"]}', "tokens": 100, "ok": True, "interrupted": None}


class WasteCase(LoopTestCase):
    def setUp(self):
        super().setUp()
        self.make_calc_project()

    def cand(self, summary="classify 분기 정리", change="internal_cleanup", symbol="classify"):
        from vmc_harness.loop import candidates
        rec, why = candidates.accept(str(self.root), {
            "summary": summary, "change": change,
            "recheck": {"type": "ast", "query": "defined calc.py::%s" % symbol},
            "anchor": {"path": "calc.py", "symbol": symbol}})
        return rec, why


class SameReasonArchiveTest(WasteCase):
    def test_same_reason_twice_archives_and_not_retried(self):
        from vmc_harness.loop import candidates, cleanse
        c, _ = self.cand()
        fake = PinOutsideTests()
        with mock.patch("vmc_harness.loop.cleanse.claude", fake):
            r1 = cleanse.process(str(self.root), ledger.get_candidate(self.root, c["candidate_id"]))
            self.assertEqual(r1["outcome"], "discarded", r1)
            self.assertEqual(ledger.get_candidate(self.root, c["candidate_id"])["status"], "open")
            r2 = cleanse.process(str(self.root), ledger.get_candidate(self.root, c["candidate_id"]))
        self.assertEqual(r2["outcome"], "archived", r2)
        self.assertEqual(r2["reason"], "고정 테스트 단계가 테스트 밖을 고침")
        cur = ledger.get_candidate(self.root, c["candidate_id"])
        self.assertEqual(cur["status"], "archived")
        self.assertEqual([a["reason"] for a in cur["attempts"]], ["고정 테스트 단계가 테스트 밖을 고침"] * 2)
        self.assertEqual([a["tokens"] for a in cur["attempts"]], [1000, 1000])
        self.assertEqual(candidates.prioritized(str(self.root), scores={}), [])  # 다시 시도하지 않는다
        n = [x for x in notices.pending(self.root) if x["kind"] == "cleansing_archived"]
        self.assertEqual(len(n), 1)
        self.assertIn("2번 연속", n[0]["text"])
        self.assertEqual(n[0]["level"], "flow")

    def test_different_reasons_and_exec_failures_do_not_archive(self):
        from vmc_harness.loop import cleanse
        c, _ = self.cand()
        results = iter([{"outcome": "failed", "reason": "정리 실패", "tokens": 5},
                        {"outcome": "failed", "reason": "로그인 필요", "tokens": 0},
                        {"outcome": "failed", "reason": "worktree: 잠김", "tokens": 0},
                        {"outcome": "discarded", "reason": "main 충돌", "tokens": 5},
                        {"outcome": "failed", "reason": "정리 실패", "tokens": 5}])
        with mock.patch("vmc_harness.loop.cleanse._process", lambda *a, **k: next(results)):
            outs = [cleanse.process(str(self.root), ledger.get_candidate(self.root, c["candidate_id"]))["outcome"]
                    for _ in range(5)]
        self.assertEqual(outs, ["failed", "failed", "failed", "discarded", "failed"])
        cur = ledger.get_candidate(self.root, c["candidate_id"])
        self.assertEqual(cur["status"], "open")
        self.assertEqual([a["reason"] for a in cur["attempts"]], ["정리 실패", "main 충돌", "정리 실패"])  # 실행 실패는 안 센다

    def test_config_key(self):
        from vmc_harness.core import config
        self.assertEqual(config.get("loop.cleanse_same_reason_max"), 2)
        self.assertEqual(config.get("loop.idle_skip_days"), 3)


class PreparedSkipTest(WasteCase):
    def test_pending_safety_net_not_reprepared_until_branch_gone(self):
        from vmc_harness.loop import cleanse
        c, _ = self.cand()
        fake = PinOnly()
        with mock.patch("vmc_harness.loop.cleanse.claude", fake):
            r1 = cleanse.process(str(self.root), c)
            self.assertEqual(r1["outcome"], "prepared", r1)  # 안전망(고정 테스트) 준비 — 후보는 open 그대로
            a1 = ledger.get_action(self.root, r1["action_id"])
            cur = ledger.get_candidate(self.root, c["candidate_id"])
            self.assertEqual(cur["status"], "open")
            r2 = cleanse.process(str(self.root), cur)
            self.assertEqual(r2, {"outcome": "skipped", "reason": "준비된 조치 머지 대기", "tokens": 0,
                                  "action_id": a1["action_id"]})
            self.assertEqual(fake.calls, 1)  # 모델을 다시 부르지 않았다
            self.assertEqual(self.git("branch", "--list", a1["branch"]).stdout.strip(), a1["branch"])  # 브랜치 그대로
            self.assertEqual(len(ledger.list_actions(self.root)), 1)
            # 브랜치가 지워졌으면(버림 등) 다시 준비한다
            self.git("branch", "-D", a1["branch"])
            r3 = cleanse.process(str(self.root), ledger.get_candidate(self.root, c["candidate_id"]))
        self.assertEqual(r3["outcome"], "prepared", r3)
        self.assertEqual(fake.calls, 2)


class DedupeTest(WasteCase):
    def test_same_anchor_and_change_not_recreated_in_any_live_status(self):
        c1, _ = self.cand(summary="unused_helper는 호출하는 곳이 없다", change="remove_unreferenced", symbol="unused_helper")
        for status in ("proposed", "prepared", "archived", "excluded"):
            ledger.update_candidate(self.root, c1["candidate_id"], status=status)
            rec, why = self.cand(summary="unused_helper는 정의만 있다(%s)" % status, change="remove_unreferenced",
                                 symbol="unused_helper")
            self.assertIsNone(rec)
            self.assertEqual(why, "이미 있는 후보(%s)" % status)
        self.assertEqual(len(ledger.list_candidates(self.root)), 1)
        # 다른 종류는 다른 후보
        rec, _ = self.cand(summary="unused_helper 고정 테스트", change="add_pinning_test", symbol="unused_helper")
        self.assertIsNotNone(rec)
        # 자연 해소·만료 뒤 다시 보이면 새로 받는다
        ledger.update_candidate(self.root, c1["candidate_id"], status="resolved")
        rec, _ = self.cand(summary="다시 생김", change="remove_unreferenced", symbol="unused_helper")
        self.assertIsNotNone(rec)
        self.assertNotEqual(rec["candidate_id"], c1["candidate_id"])

    def test_discover_prompt_lists_non_open_existing(self):
        from vmc_harness.loop import cleanse
        c1, _ = self.cand(change="remove_unreferenced", symbol="unused_helper")
        ledger.update_candidate(self.root, c1["candidate_id"], status="proposed")
        seen = {}

        def fake(root, prompt, cwd, tools, deadline=None, add_dirs=None):
            seen["p"] = prompt
            return {"text": "[]", "tokens": 10, "ok": True, "interrupted": None}
        with mock.patch("vmc_harness.loop.cleanse.claude", fake):
            cleanse.discover(str(self.root), str(self.root))
        self.assertIn("calc.py::unused_helper", seen["p"])


class IdleProjectSkipTest(WasteCase):
    def _tick(self, t):
        from vmc_harness.loop import tick
        with mock.patch("vmc_harness.loop.activity.now", return_value=t), \
                mock.patch("vmc_harness.loop.tick.now", return_value=t), \
                mock.patch("vmc_harness.loop.tick.run", return_value={"ran": True, "reason": "done"}) as run:
            return tick.tick(str(self.root)), run

    def test_never_used_project_does_not_run_and_logs_once(self):
        from vmc_harness.loop import tick
        r, run = self._tick(at(23))
        self.assertEqual(r, {"ran": False, "reason": "no_recent_human"})
        run.assert_not_called()
        self._tick(at(23, 30))
        skips = [x for x in read_jsonl(tick.runs_path(str(self.root))) if x["event"] == "skip"]
        self.assertEqual(len(skips), 1)  # 밤당 한 줄
        self.assertEqual(skips[0]["reason"], "no_recent_human")
        self.assertIsNone(skips[0]["last_human_at"])

    def test_old_session_index_only_skips_recent_prompt_runs(self):
        from vmc_harness.core.project import local_dir
        from vmc_harness.loop import activity
        idx = local_dir(self.root) / "sessions-index.jsonl"
        idx.parent.mkdir(parents=True, exist_ok=True)
        idx.write_text('{"session_id": "s", "kind": "main", "started_at": "2026-10-10T01:00:00+00:00", '
                       '"human_prompts": [{"at": "2026-10-10T02:00:00+00:00", "text": "x"}]}\n', encoding="utf-8")
        self.assertEqual(activity.last_human_at(self.root), _dt.datetime(2026, 10, 10, 2, tzinfo=_dt.timezone.utc))
        r, run = self._tick(at(23))
        self.assertEqual(r["reason"], "no_recent_human")
        activity.touch(self.root, at=at(14).isoformat())  # 오늘 사람이 시켰다
        r, run = self._tick(at(23))
        run.assert_called_once()
        self.assertEqual(run.call_args[0][1], "idle")

    def test_start_loop_bypasses(self):
        from vmc_harness.loop import activity
        activity.request_start(self.root)
        r, run = self._tick(at(23))
        run.assert_called_once()
        self.assertEqual(run.call_args[0][1], "start-loop")
