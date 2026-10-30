"""루프: OS 절전 막기·절전 설정 판정 (spec 06 실행). 실제 powercfg·pmset·SetThreadExecutionState는 모킹."""

import sys
from unittest import mock

from tests.test_loop_util import LoopTestCase

POWERCFG_EN = """Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  (Balanced)
  GUID Alias: SCHEME_BALANCED
  Subgroup GUID: 238c9fa8-0aad-41ed-83f4-97be242c8f20  (Sleep)
    GUID Alias: SUB_SLEEP
    Power Setting GUID: 29f6c1db-86da-48c5-9fdb-f2b67b1f44da  (Sleep after)
      GUID Alias: STANDBYIDLE
      Minimum Possible Setting: 0x00000000
      Maximum Possible Setting: 0xffffffff
      Possible Settings increment: 0x00000001
      Possible Settings units: Seconds
    Current AC Power Setting Index: 0x00000708
    Current DC Power Setting Index: 0x00000384
"""
POWERCFG_KO = """    전원 설정 GUID: 29f6c1db-86da-48c5-9fdb-f2b67b1f44da  (다음 시간 이후 절전)
      GUID 별칭: STANDBYIDLE
      가능한 최소 설정: 0x00000000
    현재 AC 전원 설정 인덱스: 0x00000000
    현재 DC 전원 설정 인덱스: 0x00000384
"""
PMSET = """System-wide power settings:
Currently in use:
 standby              1
 displaysleep         10
 disksleep            10
 sleep                30 (sleep prevented by coreaudiod)
 powernap             1
"""


class ParseTest(LoopTestCase):
    def test_powercfg_ac_seconds(self):
        from vmc_harness.loop import power
        self.assertEqual(power.parse_powercfg(POWERCFG_EN), 1800)
        self.assertEqual(power.parse_powercfg(POWERCFG_KO), 0)
        self.assertIsNone(power.parse_powercfg("Invalid Parameters"))

    def test_pmset_sleep_minutes(self):
        from vmc_harness.loop import power
        self.assertEqual(power.parse_pmset(PMSET), 30)  # displaysleep·disksleep과 헷갈리지 않는다
        self.assertEqual(power.parse_pmset(" sleep                0\n"), 0)
        self.assertIsNone(power.parse_pmset(""))


class CheckSleepTest(LoopTestCase):
    def _sleep_notices(self):
        from vmc_harness.core import notices
        return [n for n in notices.pending(self.root) if n["kind"] == "loop_sleep_first"]

    def test_windows_sleep_before_idle_warns(self):
        from vmc_harness.loop import power
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch("vmc_harness.loop.power.run", return_value=(0, POWERCFG_EN, "")) as m:
            r = power.check_sleep(self.root)
        self.assertEqual(m.call_args[0][0][:2], ["powercfg", "/query"])
        self.assertEqual(r, {"sleep_s": 1800, "warned": True})
        ns = self._sleep_notices()
        self.assertEqual(len(ns), 1)
        self.assertEqual(ns[0]["level"], "must")
        self.assertIn("30분 뒤 절전", ns[0]["text"])
        self.assertIn("start-loop", ns[0]["text"])

    def test_mac_sleep_never_or_long_is_quiet(self):
        from vmc_harness.loop import power
        for out in (" sleep 0\n", " sleep 180\n"):  # 안 함 / 3시간(> 2.5시간)
            with mock.patch.object(sys, "platform", "darwin"), \
                    mock.patch("vmc_harness.loop.power.run", return_value=(0, out, "")):
                self.assertFalse(power.check_sleep(self.root)["warned"])
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch("vmc_harness.loop.power.run", return_value=(0, " sleep 149\n", "")):
            self.assertTrue(power.check_sleep(self.root)["warned"])  # 2시간 29분 < 2.5시간
        self.assertEqual(len(self._sleep_notices()), 1)

    def test_unknown_is_quiet(self):
        from vmc_harness.loop import power
        for plat, ret in (("win32", (1, "", "denied")), ("darwin", (-127, "", "no pmset")), ("linux", (0, "", ""))):
            with mock.patch.object(sys, "platform", plat), mock.patch("vmc_harness.loop.power.run", return_value=ret):
                self.assertEqual(power.check_sleep(self.root), {"sleep_s": None, "warned": False})
        self.assertEqual(self._sleep_notices(), [])


class InhibitTest(LoopTestCase):
    def test_windows_set_and_clear(self):
        from vmc_harness.loop import power
        calls = []
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch("vmc_harness.loop.power._set_execution_state", side_effect=lambda f: calls.append(f) or 1):
            hold = power.inhibit()
            power.release(hold)
        self.assertTrue(hold["ok"])
        self.assertEqual(calls, [power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED, power.ES_CONTINUOUS])

    def test_failure_does_not_raise(self):
        from vmc_harness.loop import power
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch("vmc_harness.loop.power._set_execution_state", side_effect=OSError("no kernel32")):
            hold = power.inhibit()
        self.assertFalse(hold["ok"])
        self.assertIn("kernel32", hold["reason"])
        power.release(hold)
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch("vmc_harness.loop.power.subprocess.Popen", side_effect=FileNotFoundError("caffeinate")):
            self.assertFalse(power.inhibit()["ok"])

    def test_mac_caffeinate_child(self):
        from vmc_harness.loop import power
        fake = mock.Mock()
        fake.poll.return_value = None
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch("vmc_harness.loop.power.subprocess.Popen", return_value=fake) as P:
            hold = power.inhibit()
            power.release(hold)
        self.assertEqual(P.call_args[0][0][:3], ["caffeinate", "-i", "-w"])
        fake.terminate.assert_called_once()
        self.assertNotIn("_handle", power.summary(hold))

    def test_run_holds_and_releases(self):
        from vmc_harness.loop import tick
        events = []
        with mock.patch("vmc_harness.loop.power.inhibit",
                        side_effect=lambda: events.append("hold") or {"ok": False, "reason": "x"}), \
                mock.patch("vmc_harness.loop.power.release", side_effect=lambda h: events.append("release")), \
                mock.patch("vmc_harness.loop.tick.gitops.main_branch", side_effect=RuntimeError("boom")):
            r = tick.run(str(self.root), "manual")
        self.assertEqual(r["reason"], "error")  # 절전 막기 실패해도 루프는 진행(고장은 기록)
        self.assertEqual(events, ["hold", "release"])
        start = [x for x in tick.night_records(str(self.root)) if x["event"] == "start"][-1]
        self.assertEqual(start["power"], {"ok": False, "reason": "x"})
