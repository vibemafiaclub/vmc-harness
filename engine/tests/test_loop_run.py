"""루프: 활동·퇴근 판정 · 스케줄러 내용 · 처리 단위(가짜 claude) · 아침 머지 · tick · 훅."""

import datetime as _dt
import os
import plistlib
import sys
from unittest import mock

from tests.test_loop_util import CALC_REFACTORED, PIN_TEST, LoopTestCase
from vmc_harness.core import ledger
from vmc_harness.core.jsonio import read_jsonl, write_json
from vmc_harness.hooks.contract import HookContext


def at(h, m=0, day=22):
    return _dt.datetime(2026, 10, day, h, m).astimezone()


class ActivityTest(LoopTestCase):
    def test_idle_window(self):
        from vmc_harness.loop import activity
        activity.touch(self.root, at=at(14, 0).isoformat())
        self.assertFalse(activity.is_idle(self.root, at(17, 59)))  # 18시 전
        self.assertTrue(activity.is_idle(self.root, at(18, 0)))  # 18시 이후 4시간 무지시
        activity.touch(self.root, at=at(17, 30).isoformat())
        self.assertFalse(activity.is_idle(self.root, at(19, 0)))  # 1.5시간
        self.assertFalse(activity.is_idle(self.root, at(19, 59)))  # 2.5시간 전(정수 2로 자르지 않는다)
        self.assertTrue(activity.is_idle(self.root, at(20, 0)))  # 2.5시간
        self.assertTrue(activity.is_idle(self.root, at(3, 0, day=23)))
        self.assertFalse(activity.is_idle(self.root, at(7, 0, day=23)))  # stop_hour
        self.assertEqual(activity.night_key(self.root, at(3, 0, day=23)), "2026-10-22")
        self.assertEqual(activity.next_stop_at(self.root, at(20, 0)), at(7, 0, day=23))

    def test_touch_signals_stop_only_when_running(self):
        from vmc_harness.loop import activity
        activity.touch(self.root)
        self.assertFalse(activity.stop_requested(self.root))
        activity.mark_running(self.root, {"pid": os.getpid()})
        activity.touch(self.root)
        self.assertTrue(activity.stop_requested(self.root))


class SchedulerTest(LoopTestCase):
    def test_plist(self):
        from vmc_harness.loop import scheduler
        d = plistlib.loads(scheduler.plist_content(self.root))
        self.assertEqual(d["StartInterval"], 1800)
        self.assertFalse(d["RunAtLoad"])
        self.assertIn("tick", d["ProgramArguments"])
        self.assertEqual(d["ProgramArguments"][-2:], ["--root", str(self.root)])
        self.assertTrue(d["Label"].startswith("com.vibemafiaclub.vmc-harness.loop."))
        self.assertTrue(d["StandardOutPath"].startswith(str(self.home)))

    def test_windows_args_user_scope(self):
        from vmc_harness.loop import scheduler
        args = scheduler.windows_create_args(self.root)
        self.assertEqual(args[:7], ["schtasks", "/Create", "/SC", "MINUTE", "/MO", "30", "/TN"])
        self.assertNotIn("/RU", args)
        self.assertNotIn("/RL", args)
        self.assertIn("loop-tick.pyw", args[args.index("/TR") + 1])
        self.assertIn("main(cmd[3:])", scheduler.pyw_content(self.root))

    def test_windows_xml_battery_wake(self):
        import xml.etree.ElementTree as ET
        from vmc_harness.loop import scheduler
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        doc = ET.fromstring(scheduler.windows_task_xml(self.root).encode("utf-16"))
        st = doc.find("t:Settings", ns)
        self.assertEqual(st.find("t:DisallowStartIfOnBatteries", ns).text, "false")
        self.assertEqual(st.find("t:StopIfGoingOnBatteries", ns).text, "false")
        self.assertEqual(st.find("t:WakeToRun", ns).text, "true")
        self.assertEqual(st.find("t:ExecutionTimeLimit", ns).text, "PT14H")  # 18시~7시 + 1
        pr = doc.find("t:Principals/t:Principal", ns)
        self.assertEqual(pr.find("t:LogonType", ns).text, "InteractiveToken")
        self.assertEqual(pr.find("t:RunLevel", ns).text, "LeastPrivilege")
        self.assertEqual(doc.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", ns).text, "PT30M")
        self.assertIn("loop-tick.pyw", doc.find("t:Actions/t:Exec/t:Arguments", ns).text)
        args = scheduler.windows_xml_args(self.root)
        self.assertEqual(args[:3], ["schtasks", "/Create", "/XML"])
        self.assertNotIn("/RU", args)

    def test_windows_register_xml_utf16(self):
        from vmc_harness.loop import scheduler
        calls = []
        with mock.patch.object(sys, "platform", "win32"), mock.patch("vmc_harness.loop.scheduler.shutil.which", return_value=None), \
                mock.patch("vmc_harness.loop.scheduler.run", side_effect=lambda a: calls.append(a) or (0, "", "")):
            r = scheduler.register(self.root)
        self.assertEqual((r["method"], r["variant"]), ("schtasks", "xml"))
        self.assertEqual(len(calls), 1)
        self.assertIn("/XML", calls[0])
        raw = scheduler.xml_path(self.root).read_bytes()
        self.assertIn(raw[:2], (b"\xff\xfe", b"\xfe\xff"))  # UTF-16 BOM
        self.assertIn("WakeToRun", raw.decode("utf-16"))
        self.assertEqual(scheduler.state(self.root)["variant"], "xml")

    def test_windows_register_xml_fails_falls_back_to_minute(self):
        from vmc_harness.loop import scheduler
        calls = []

        def fake(a):
            calls.append(a)
            return (1, "", "ERROR: access denied") if "/XML" in a else (0, "", "")
        with mock.patch.object(sys, "platform", "win32"), mock.patch("vmc_harness.loop.scheduler.shutil.which", return_value=None), \
                mock.patch("vmc_harness.loop.scheduler.run", side_effect=fake):
            r = scheduler.register(self.root)
        self.assertEqual((r["ok"], r["variant"]), (True, "minute"))
        self.assertEqual(calls[1][:4], ["schtasks", "/Create", "/SC", "MINUTE"])
        self.assertIn("access denied", scheduler.state(self.root)["reason"])

    def test_manual_fallback_notice(self):
        from vmc_harness.core import notices
        from vmc_harness.loop import scheduler
        with mock.patch.object(sys, "platform", "linux"):
            r = scheduler.register(self.root)
        self.assertEqual(r["method"], "manual")
        self.assertEqual(scheduler.state(self.root)["method"], "manual")
        self.assertTrue(any(n["kind"] == "loop_scheduler_manual" and "start-loop" in n["text"]
                            for n in notices.pending(self.root)))

    def test_launchctl_failure_falls_back(self):
        from vmc_harness.loop import scheduler
        fake_home = self._tmp / "fakehome"
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch("vmc_harness.loop.scheduler.Path.home", return_value=fake_home), \
                mock.patch("vmc_harness.loop.scheduler.run", return_value=(5, "", "Operation not permitted")):
            r = scheduler.register(self.root)
        self.assertEqual(r["method"], "manual")
        self.assertFalse(list(fake_home.rglob("*.plist")))


class FakeClaude:
    """프롬프트 종류에 따라 worktree 파일을 고치는 가짜 claude -p."""

    def __init__(self, pin=PIN_TEST, change=CALC_REFACTORED, stop_root=None):
        self.pin, self.change, self.stop_root, self.calls = pin, change, stop_root, []

    def __call__(self, root, prompt, cwd, tools, deadline=None, add_dirs=None):
        from pathlib import Path
        self.calls.append(prompt[:20])
        if "현재 동작을 고정" in prompt:
            p = Path(cwd) / "tests" / "test_calc.py"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(self.pin, encoding="utf-8")
            return {"text": '{"tests": ["tests/test_calc.py"]}', "tokens": 100, "ok": True, "interrupted": None}
        if self.stop_root:
            from vmc_harness.loop import activity
            activity.signal_stop(self.stop_root, "human_prompt")
            return {"text": "", "tokens": 50, "ok": False, "interrupted": "stop"}
        (Path(cwd) / "calc.py").write_text(self.change, encoding="utf-8")
        return {"text": '{"done": true}', "tokens": 200, "ok": True, "interrupted": None}


class ProcessTest(LoopTestCase):
    def cand(self, change="internal_cleanup"):
        from vmc_harness.loop import candidates
        rec, why = candidates.accept(str(self.root), {
            "summary": "classify 분기 정리", "change": change,
            "recheck": {"type": "ast", "query": "defined calc.py::classify"},
            "anchor": {"path": "calc.py", "symbol": "classify"}})
        self.assertIsNotNone(rec, why)
        return rec

    def test_safety_net_first_then_B(self):
        from vmc_harness.loop import cleanse
        self.make_calc_project()
        c = self.cand()
        fake = FakeClaude()
        with mock.patch("vmc_harness.loop.cleanse.claude", fake):
            r = cleanse.process(str(self.root), c)
        # 테스트가 없는 영역 → 고정 테스트 추가(A)로 바뀜
        self.assertEqual(r["outcome"], "prepared", r)
        self.assertEqual(r["grade"], "A")
        a = ledger.get_action(self.root, r["action_id"])
        self.assertEqual(a["status"], "prepared")
        self.assertEqual(a["change"], "add_pinning_test")
        self.assertEqual(a["basis"], {"type": "cleansing", "ref": c["candidate_id"]})
        msg = self.git("log", "-1", "--format=%B", a["branch"]).stdout
        self.assertIn("Harness-Action: %s" % a["action_id"], msg)
        self.assertEqual(ledger.list_candidates(self.root)[0]["status"], "open")  # 정리는 그물 뒤
        self.assertFalse((self.root / "tests" / "test_calc.py").exists())  # 개발자 작업 트리 그대로
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "main")

    def test_covered_area_goes_B(self):
        from vmc_harness.loop import cleanse
        self.make_calc_project()
        self.write("tests/test_existing.py", "from calc import classify\n")
        self.commit_old("existing test")
        self.qa_pass("calc-page", ["calc.py"])
        self.commit_old("qa")
        c = self.cand()
        with mock.patch("vmc_harness.loop.cleanse.claude", FakeClaude()):
            r = cleanse.process(str(self.root), c)
        self.assertEqual(r["outcome"], "prepared", r)
        self.assertEqual(r["grade"], "B")
        a = ledger.get_action(self.root, r["action_id"])
        self.assertEqual(a["fixation"], {"1": True, "2": True, "3": True, "4": True})
        self.assertEqual(len(a["commits"]), 2)  # 고정 테스트 커밋 → 정리 커밋
        self.assertEqual(a["target"], {"area": "calc.py"})
        # 같은 영역에 머지 대기 조치가 있으면 다음 후보는 건너뜀
        from vmc_harness.loop import candidates
        c2, _ = candidates.accept(str(self.root), {"summary": "다른 정리", "change": "internal_cleanup",
                                                   "recheck": {"type": "ast", "query": "defined calc.py::unused_helper"},
                                                   "anchor": {"path": "calc.py", "symbol": "unused_helper"}})
        self.assertEqual(cleanse.process(str(self.root), c2)["outcome"], "skipped")

    def test_propose_structure_change(self):
        from vmc_harness.core import notices
        from vmc_harness.loop import cleanse
        self.make_calc_project()
        c = self.cand(change="move")
        r = cleanse.process(str(self.root), c)
        self.assertEqual(r["outcome"], "proposed")
        n = [x for x in notices.pending(self.root, area="calc.py") if x["kind"] == "cleansing_proposal"]
        self.assertEqual(len(n), 1)
        self.assertEqual(n[0]["level"], "flow")
        self.assertEqual(notices.pending(self.root, area="other/x.py"), [])

    def test_yield_on_human_return(self):
        from vmc_harness.loop import cleanse
        self.make_calc_project()
        self.write("tests/test_existing.py", "from calc import classify\n")
        self.commit_old("existing test")
        c = self.cand()
        with mock.patch("vmc_harness.loop.cleanse.claude", FakeClaude(stop_root=str(self.root))):
            r = cleanse.process(str(self.root), c)
        self.assertEqual(r["outcome"], "yielded")
        self.assertEqual(ledger.list_candidates(self.root)[0]["status"], "open")  # 대기열로
        self.assertEqual(self.git("branch", "--list", "vmc-cleanse/*").stdout.strip(), "")

    def test_natural_resolution(self):
        from vmc_harness.loop import cleanse
        self.make_calc_project()
        c = self.cand()
        os.remove(str(self.root / "calc.py"))
        self.commit_old("drop")
        r = cleanse.process(str(self.root), c)
        self.assertEqual(r["outcome"], "resolved")


class MorningMergeTest(LoopTestCase):
    def prepare(self):
        from vmc_harness.loop import cleanse
        self.make_calc_project()
        c = ProcessTest.cand(self)
        with mock.patch("vmc_harness.loop.cleanse.claude", FakeClaude()):
            r = cleanse.process(str(self.root), c)
        self.assertEqual(r["outcome"], "prepared")
        return r["action_id"]

    def test_merge_fast_forward_and_notice(self):
        from vmc_harness.core import notices
        from vmc_harness.loop import merge
        aid = self.prepare()
        out = merge.morning(str(self.root))
        self.assertEqual(out[0]["result"], "merged", out)
        a = ledger.get_action(self.root, aid)
        self.assertEqual(a["status"], "trial")  # 머지 뒤 ledger 판정 대상
        self.assertTrue(a["merged_at"])
        self.assertTrue(a["trial"]["deadline"])
        self.assertEqual(a["target"], {"area": "tests/test_calc.py"})
        from vmc_harness.ledger import exposure
        self.assertIn(aid, exposure.matching_actions(self.root, area="tests/test_calc.py"))
        self.assertTrue((self.root / "tests" / "test_calc.py").exists())
        self.assertIn("Harness-Action: %s" % aid, self.git("log", "-1", "--format=%B").stdout)
        self.assertTrue(any(n["kind"] == "cleansing_merged" for n in notices.pending(self.root)))
        self.assertEqual(self.git("branch", "--list", "vmc-cleanse/*").stdout.strip(), "")

    def test_conflict_discards_back_to_queue(self):
        from vmc_harness.loop import merge
        aid = self.prepare()
        self.write("tests/test_calc.py", "# 사람이 먼저 만든 파일\n")
        self.commit_old("human test")
        out = merge.morning(str(self.root))
        self.assertEqual(out[0]["result"], "discarded")
        self.assertTrue(ledger.get_action(self.root, aid)["discarded"])
        self.assertEqual(ledger.list_candidates(self.root)[0]["status"], "open")

    def test_qa_regression_blocks(self):
        from vmc_harness.loop import merge
        from vmc_harness.qa import results
        aid = self.prepare()
        self.qa_pass("calc-page", ["tests/**", "calc.py"])
        results.begin(self.root, "calc-page")
        results.set_status(self.root, "calc-page", "FAIL", cause="회귀", needs_human=False, needs_db_change=False)
        out = merge.morning(str(self.root))
        self.assertEqual(out[0]["result"], "blocked")
        self.assertEqual(ledger.get_action(self.root, aid)["status"], "prepared")

    def test_gxp_no_merge(self):
        from vmc_harness.loop import merge
        self.prepare()
        write_json(self.root / ".harness/config.json", {"project_id": self.project_id, "profile": "gxp"})
        self.assertEqual(merge.morning(str(self.root)), [])


class TickTest(LoopTestCase):
    def setUp(self):
        super().setUp()
        self.make_calc_project()

    def test_disabled_in_gxp(self):
        from vmc_harness.loop import tick
        write_json(self.root / ".harness/config.json", {"project_id": self.project_id, "profile": "gxp"})
        self.assertEqual(tick.tick(str(self.root))["reason"], "disabled")

    def test_not_idle_in_daytime(self):
        from vmc_harness.loop import activity, tick
        activity.touch(self.root)
        with mock.patch("vmc_harness.loop.activity.now", return_value=at(14)):
            self.assertEqual(tick.tick(str(self.root))["reason"], "not_idle")

    def test_run_logs_and_token_cap(self):
        from vmc_harness.loop import activity, candidates, tick
        for name in ("a", "b", "c"):
            self.write("%s.py" % name, "def f_%s():\n    return 1\n" % name)
        self.commit_old("more")
        for name in ("a", "b", "c"):
            candidates.accept(str(self.root), {"summary": name, "change": "remove_unreferenced",
                                               "recheck": {"type": "ast", "query": "unreferenced %s.py::f_%s" % (name, name)},
                                               "anchor": {"path": "%s.py" % name, "symbol": "f_%s" % name}})
        activity.request_start(self.root)
        calls = []

        def fake_process(root, cand, deadline=None):
            calls.append(cand["candidate_id"])
            return {"outcome": "proposed", "tokens": 600000}

        with mock.patch("vmc_harness.loop.cleanse.process", fake_process), \
                mock.patch("vmc_harness.loop.cleanse.discover", return_value=(0, 1000, None)):
            r = tick.tick(str(self.root))
        self.assertTrue(r["ran"])
        self.assertEqual(r["reason"], "token_cap")
        self.assertEqual(len(calls), 2)  # 100만 상한: 2건 뒤 멈춤
        recs = read_jsonl(tick.runs_path(str(self.root)))
        self.assertEqual([x["event"] for x in recs], ["start", "discover", "sync", "unit", "unit", "end"])
        self.assertFalse(activity.is_running(self.root))
        self.assertEqual(tick.tick(str(self.root))["reason"], "not_idle" if not activity.is_idle(self.root) else "done_tonight")

    def test_yield_reason(self):
        from vmc_harness.loop import candidates, tick
        candidates.accept(str(self.root), {"summary": "x", "change": "remove_unreferenced",
                                           "recheck": {"type": "ast", "query": "unreferenced calc.py::unused_helper"},
                                           "anchor": {"path": "calc.py", "symbol": "unused_helper"}})

        def fake_process(root, cand, deadline=None):
            return {"outcome": "yielded", "reason": "stop", "tokens": 10}

        with mock.patch("vmc_harness.loop.cleanse.process", fake_process), \
                mock.patch("vmc_harness.loop.cleanse.discover", return_value=(0, 0, None)):
            r = tick.run(str(self.root), "start-loop")
        self.assertEqual(r["reason"], "yielded")


class HandlersTest(LoopTestCase):
    def ctx(self, event):
        return HookContext(event=event, agent="claude", raw={}, root=str(self.root))

    def test_prompt_touch_and_nested(self):
        from vmc_harness.loop import activity, handlers
        handlers.on_user_prompt(self.ctx("UserPromptSubmit"))
        self.assertTrue(activity.load(self.root).get("last_prompt_at"))
        os.environ["VMC_HARNESS_NESTED"] = "1"
        activity.mark_running(self.root, {"pid": os.getpid()})
        handlers.on_user_prompt(self.ctx("UserPromptSubmit"))
        self.assertFalse(activity.stop_requested(self.root))  # 루프 자신의 세션은 양보 신호를 안 낸다

    def test_session_start_spawns_merge(self):
        from vmc_harness.loop import handlers
        self.make_calc_project()
        ledger.create_action(self.root, {"type": "cleansing", "ref": "C-1"}, "code", "h", status="prepared",
                             extra={"grade": "A", "branch": "vmc-cleanse/x"})
        with mock.patch("vmc_harness.core.proc.spawn_detached") as sp:
            r = handlers.on_session_start(self.ctx("SessionStart"))
        self.assertIsNone(r)  # 세션 시작에 띄우지 않는다(08) — 결과는 알림으로
        args = sp.call_args[0][0]
        self.assertEqual(args[-4:-2], ["loop", "merge"])
