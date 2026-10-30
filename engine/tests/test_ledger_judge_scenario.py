"""시간을 압축한 학습·판정 시나리오 (spec 04 — 0.1.4 현장 결함 재현, 합성 데이터만).

가짜 시계로 며칠치 노출·판정·밤 루프·백그라운드 실패를 돌린다.
- 조건 없는 생성 게이트: 무관한 도구 호출은 노출이 아니다. 조건이 해당(위반)한 때만 노출, 최소 기간 전엔 존속 안 함
- 0.1.4 장부(모든 호출을 노출로 센 존속·원복됐는데 켜진 게이트)는 업데이트 뒤 첫 판정에서 바로잡힌다
- 세션 시작 분리 실행이 죽어도: 실패가 events·알림에 남고, 밤 루프 tick이 대신 판정하고, 멈춤은 '학습이 멈췄다' 한 줄
- 손대지 않은 제안 후보는 14일 뒤 만료
- 카드 검색(MCP search_cards) 반환 → 노출 → 집계 → 판정
- Windows 분리 실행 플래그(창 없음 + job 이탈 실패 시 재시도)
"""

import datetime as _dt
import json
import os
import sys
from pathlib import Path
from unittest import mock

from tests.gates_util import GatesCase
from vmc_harness.core import clock, config, events, ledger, notices
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.gates import generated
from vmc_harness.ledger import health, judge, metrics

GEN = [("vmc_harness.gates.generated", False)]
# 조건 없는 게이트(trigger에 paths·change_kinds 없음) — 본체가 명령을 보고 판단한다
BODY = '''def check(api):
    if api.command and "DELETE FROM" in api.command.upper():
        return "DB 직접 삭제"
'''
BASIS = {"type": "machine_fact", "command": "cat db/README.md", "value": "운영 DB 직접 삭제 금지"}


class FakeClock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t = self.t + _dt.timedelta(**kw)
        return self.t


def install_clock(test, start):
    """clock.now와, 그걸 이름으로 가져간 모든 vmc_harness 모듈의 now를 가짜 시계로."""
    fake = FakeClock(start)
    orig = clock.now
    for name, mod in list(sys.modules.items()):
        if name.startswith("vmc_harness") and getattr(mod, "now", None) is orig:
            p = mock.patch.object(mod, "now", fake)
            p.start()
            test.addCleanup(p.stop)
    return fake


START = _dt.datetime(2026, 10, 23, 9, 0).astimezone()


class Scenario(GatesCase):
    def setUp(self):
        super().setUp()
        self.write("README.md", "x\n")
        self.commit("base")
        self.clock = install_clock(self, START)

    # ---- 도우미 ----

    def make_gate(self, title="DB 직접 삭제 금지", trigger=None):
        return generated.create(self.root, title=title, basis=BASIS, trigger=trigger or {"events": ["PreToolUse"]},
                                script_text=BODY, measure=False)["meta"]

    def bash(self, cmd):
        return self.hook("PreToolUse", "Bash", {"command": cmd}, modules=GEN)

    def gate_file(self, gid):
        return read_json(self.root / ".harness" / "gates" / ("%s.json" % gid))

    def notice_kinds(self):
        return [n["kind"] for n in notices.pending(self.root)]

    # ---- ④ 노출 정의 ----

    def test_condition_less_gate_counts_only_hits_and_waits_min_days(self):
        m = self.make_gate()
        aid = m["action_id"]
        for day in range(2):  # 이틀 동안 무관한 호출 수백 번(0.1.4는 이걸 다 노출로 셌다 → 17시간 만에 존속)
            for _ in range(150):
                self.assertAllowed(self.bash("ls -la"))
            self.clock.advance(hours=12)
            judge.run(self.root)
            self.clock.advance(hours=12)
        self.assertEqual(judge.progress(self.root)[0]["exposures"], 0)
        self.assertEqual(ledger.get_action(self.root, aid)["status"], "trial")

        need = config.get("judge.exposures", self.root)
        for _ in range(need):  # 조건이 실제로 해당 — 막음
            self.assertDenied(self.bash("sqlcmd -Q \"DELETE FROM t\""), "DB 직접 삭제")
        judge.run(self.root)
        self.assertEqual(judge.progress(self.root)[0]["exposures"], need)
        # 2일째 — 노출은 찼지만 최소 기간(judge.min_days) 전이라 아직 시험 중
        self.assertEqual(ledger.get_action(self.root, aid)["status"], "trial")
        self.clock.advance(days=config.get("judge.min_days", self.root))
        vs = judge.run(self.root)
        self.assertEqual([v["result"] for v in vs], ["kept"])
        self.assertEqual(ledger.get_action(self.root, aid)["status"], "kept")
        g = self.gate_file(m["gate_id"])
        self.assertEqual(g["status"], "kept")       # 판정이 게이트 파일에도
        self.assertTrue(g.get("enabled", True))

    def test_false_blocks_revert_and_disable_gate_file(self):
        m = self.make_gate()
        for _ in range(config.get("judge.false_block_immediate", self.root) + 1):
            ledger.create_finding(self.root, "false_block", {"gate_id": m["gate_id"]})
        self.clock.advance(hours=1)
        judge.run(self.root)
        self.assertEqual(ledger.get_action(self.root, m["action_id"])["status"], "reverted")
        g = self.gate_file(m["gate_id"])
        self.assertEqual(g["status"], "reverted")
        self.assertFalse(g["enabled"])               # 원복인데 enabled true로 남지 않는다
        self.assertFalse(generated.active(g))

    # ---- 0.1.4 장부 바로잡기 ----

    def _old_kept(self, m, exposures=608, blocked=0, day="2026-10-24"):
        aid = m["action_id"]
        ledger.update_action(self.root, aid, status="kept", trial={"exposures": exposures, "negatives": 0})
        ledger.write_verdict(self.root, {"action_id": aid, "result": "kept", "exposures": exposures,
                                         "reason": "노출 %d회 동안 명시적 부정 0" % exposures})
        write_json(ledger.metrics_path(self.root, day, "old-pc"),
                   {"pc_id": "old-pc", "date": day, "exposures": {aid: exposures},
                    "gates": {m["gate_id"]: {"fired": exposures, "blocked": blocked, "false_block": 0, "sec": 1.0}}})

    def test_update_reopens_baseless_kept_and_fixes_gate_files(self):
        a = self.make_gate("옛 존속 — 막은 적 없음")
        b = self.make_gate("옛 존속 — 실제로 막음")
        c = self.make_gate("옛 원복 — 켜진 채")
        d = self.make_gate("옛 시험 중 — 노출 부풀림")
        self._old_kept(a, blocked=0)
        self._old_kept(b, exposures=40, blocked=config.get("judge.min_hits", self.root))
        ledger.update_action(self.root, c["action_id"], status="reverted")
        gc = self.gate_file(c["gate_id"])
        gc.update(status="reverted", enabled=True)
        write_json(self.root / ".harness" / "gates" / ("%s.json" % c["gate_id"]), gc)
        write_json(ledger.metrics_path(self.root, "2026-10-23", "old-pc"),
                   {"pc_id": "old-pc", "date": "2026-10-23", "exposures": {d["action_id"]: 300},
                    "gates": {d["gate_id"]: {"fired": 300, "blocked": 0, "false_block": 0, "sec": 1.0}}})

        self.clock.advance(days=3)
        judge.run(self.root)   # 업데이트 뒤 첫 판정

        ra = ledger.get_action(self.root, a["action_id"])
        self.assertEqual(ra["status"], "trial")
        self.assertIn("reopened", ra["trial"])
        self.assertEqual(self.gate_file(a["gate_id"])["status"], "trial")
        v = [x for x in ledger.list_verdicts(self.root) if x["action_id"] == a["action_id"]][0]
        self.assertEqual(v["superseded"]["result"], "trial")
        self.assertTrue([e for e in events.read(self.root, "verdict_reopened") if e["action_id"] == a["action_id"]])
        self.assertEqual(ledger.get_action(self.root, b["action_id"])["status"], "kept")
        self.assertEqual(self.gate_file(b["gate_id"])["status"], "kept")       # 존속인데 파일은 trial이던 것도 맞춘다
        self.assertFalse(self.gate_file(c["gate_id"])["enabled"])
        rd = ledger.get_action(self.root, d["action_id"])
        self.assertEqual(rd["status"], "trial")
        self.assertEqual([p for p in judge.progress(self.root) if p["action_id"] == d["action_id"]][0]["exposures"], 0)

        # 두 번째 판정에서는 다시 바로잡지 않는다(한 번만)
        self.clock.advance(days=1)
        judge.run(self.root)
        self.assertEqual(len(events.read(self.root, "verdict_reopened")), 1)
        # 다시 연 시험: 다음 날부터 새 정의(해당)로 센다 → 최소 기간 뒤 존속
        from vmc_harness.ledger import exposure
        for _ in range(config.get("judge.exposures", self.root)):  # 새 정의의 노출(조건 해당) — 게이트가 직접 남기는 것과 같은 기록
            exposure.record(self.root, gate_id=a["gate_id"])
        self.clock.advance(days=config.get("judge.min_days", self.root))
        judge.run(self.root)
        self.assertEqual(ledger.get_action(self.root, a["action_id"])["status"], "kept")

    # ---- ② 백그라운드 실패 · 밤 루프 · 건강 신호 ----

    def test_background_dies_night_loop_judges_and_health_signals(self):
        from vmc_harness.ledger import handlers
        from vmc_harness.loop import tick
        m = self.make_gate()
        spawned = []
        ctx = mock.Mock(root=self.root, session_id="s-1")
        # 세션 시작 분리 실행: 프로세스가 기동되지 않는 PC(0.1.4 현장처럼 집계가 멈춤)
        with mock.patch.object(handlers, "spawn_cli", lambda args, cwd=None: spawned.append(args)):
            handlers.on_session_start(ctx)
            self.assertEqual(len(spawned), 1)
            self.clock.advance(minutes=10)
            handlers.on_session_start(ctx)
            self.assertEqual(len(spawned), 1)        # 도는 중(유예 안) — 다시 띄우지 않는다
            self.clock.advance(minutes=config.get("judge.spawn_grace_min", self.root))
            handlers.on_session_start(ctx)
            self.assertEqual(len(spawned), 2)        # 유예가 지나면 실패로 보고 다시
        errs = events.read(self.root, "ledger_bg_error")
        self.assertEqual(len(errs), 1)
        self.assertIn("시작되지 않음", errs[0]["error"])
        self.assertIn(health.FAIL_KIND, self.notice_kinds())

        # 사흘 동안 계속 쓰지만(훅 이벤트) 분리 실행은 계속 죽는다 → 학습이 멈췄다
        with mock.patch.object(handlers, "spawn_cli", lambda args, cwd=None: spawned.append(args)):
            for _ in range(6):
                self.clock.advance(hours=12)
                for _ in range(3):
                    self.assertDenied(self.bash("sqlcmd -Q \"DELETE FROM t\""))
                ledger.append_task(self.root, {"task_id": "T-%s" % clock.now_iso(), "at": clock.now_iso()})
                handlers.on_session_start(ctx)
        st = health.load(self.root)
        self.assertIn("판정", st["stalled"])
        stall = [n for n in notices.pending(self.root) if n["kind"] == health.STALL_KIND]
        self.assertEqual(len(stall), 1)              # 같은 멈춤은 한 번만
        self.assertIn("학습이 멈췄다", stall[0]["text"])
        self.assertEqual(ledger.get_action(self.root, m["action_id"])["status"], "trial")  # 판정이 안 돌았다

        # 밤 루프 tick이 대신 집계·판정(루프 본체는 이미 도는 중이라 건너뜀)
        self.clock.advance(minutes=config.get("judge.spawn_grace_min", self.root) + 1)
        from vmc_harness.loop import activity
        with mock.patch.object(activity, "is_running", lambda root: True):
            r = tick.tick(self.root)
        self.assertEqual(r["reason"], "running")
        self.assertEqual(ledger.get_action(self.root, m["action_id"])["status"], "kept")
        self.assertEqual(self.gate_file(m["gate_id"])["status"], "kept")
        st = health.load(self.root)
        self.assertEqual(st["stalled"], [])
        self.assertTrue(st.get("judge_last_run") and st.get("rollup_last_run"))

    def test_ledger_stall_alone_is_signalled_and_rest_days_are_not(self):
        ledger.append_task(self.root, {"task_id": "T-1", "at": clock.now_iso()})
        judge.run(self.root)
        self.clock.advance(days=4)               # 쉬는 날: 하네스를 안 썼으면 멈춤이 아니다
        self.assertEqual(health.check(self.root)["stalled"], [])
        for _ in range(3):                       # 다시 쓰는데 작업 장부만 안 쓰인다(판정은 매일 돈다)
            events.log(self.root, "gate_fire", gate_id="kernel:x")
            judge.run(self.root)
            self.clock.advance(days=1)
        events.log(self.root, "gate_fire", gate_id="kernel:x")
        self.assertEqual(health.check(self.root)["stalled"], ["작업 장부"])

    def test_per_task_ledger_files_count_as_ledger_writes(self):
        # 0.1.5 장부는 작업당 파일(write_task) — 옛 jsonl만 보면 쓰는 중에도 "멈춤"으로 잘못 알린다
        judge.run(self.root)
        for i in range(4):
            events.log(self.root, "gate_fire", gate_id="kernel:x")
            ledger.write_task(self.root, {"task_id": "T-%d" % i, "started_at": clock.now_iso(), "status": "open"})
            judge.run(self.root)
            self.clock.advance(days=1)
        events.log(self.root, "gate_fire", gate_id="kernel:x")
        self.assertEqual(health.check(self.root)["stalled"], [])

    def test_background_job_error_is_recorded(self):
        from vmc_harness.ledger import handlers
        with mock.patch.object(judge, "run", side_effect=RuntimeError("장부 파일 잠김")):
            out = handlers.background_job(self.root)
        self.assertIn("judge_error", out)
        e = [x for x in events.read(self.root, "ledger_bg_error") if x["stage"] == "judge"]
        self.assertEqual(len(e), 1)
        self.assertEqual(health.load(self.root)["last_error"]["stage"], "judge")

    # ---- A3 제안 후보 만료 ----

    def test_untouched_proposed_candidates_expire(self):
        from vmc_harness.loop import candidates
        c1 = ledger.create_candidate(self.root, "cleansing", "모듈 나누기", recheck={"type": "grep", "query": "x"},
                                     anchor={"path": "README.md", "symbol": None})
        ledger.update_candidate(self.root, c1["candidate_id"], status="proposed", proposed_at=clock.now_iso())
        c2 = ledger.create_candidate(self.root, "cleansing", "열린 후보", recheck={"type": "grep", "query": "x"},
                                     anchor={"path": "README.md", "symbol": None})
        days = config.get("loop.proposed_expire_days", self.root)
        self.clock.advance(days=days - 1)
        self.assertEqual(candidates.expire_proposed(self.root), [])
        self.clock.advance(days=1)
        self.assertEqual(candidates.expire_proposed(self.root), [c1["candidate_id"]])
        self.assertEqual(ledger.get_candidate(self.root, c1["candidate_id"])["status"], "expired")
        self.assertEqual(ledger.get_candidate(self.root, c2["candidate_id"])["status"], "open")

    # ---- 카드 노출 경로(MCP) ----

    def test_mcp_card_search_records_exposure_to_judgment(self):
        from vmc_harness.brain import mcp_server, search
        card = {"id": "K-test-1", "title": "UI 동작은 확인하고 제안", "signals": ["열림 방향"], "judgment": "실제로 확인한다"}
        a = ledger.create_action(self.root, {"type": "learning", "ref": "C-x"}, "card", "정정이 준다",
                                 why_not_higher="판단이라 카드", target={"card": card["id"]})
        os.environ["CLAUDE_PROJECT_DIR"] = str(self.root)   # 서버가 프로젝트 밖 폴더에서 떠도 찾는다
        elsewhere = self._tmp / "elsewhere"
        elsewhere.mkdir()
        old = os.getcwd()
        os.chdir(str(elsewhere))
        try:
            root = mcp_server._root()
        finally:
            os.chdir(old)
        self.assertEqual(Path(root).resolve(), self.root.resolve())
        with mock.patch.object(search, "active_cards", lambda: [card]), \
                mock.patch.object(search, "select_llm", lambda q, c, k: None):
            for _ in range(config.get("judge.exposures", self.root)):
                resp = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                          "params": {"name": "search_cards",
                                                     "arguments": {"situation": "UI 열림 방향 제안"}}}, root)
                self.assertIn("K-test-1", json.dumps(resp, ensure_ascii=False))
                self.clock.advance(hours=12)
        judge.run(self.root)
        self.assertEqual(ledger.get_action(self.root, a["action_id"])["status"], "kept")
        self.assertEqual(metrics.card_uses(self.root, card["id"]), config.get("judge.exposures", self.root))

        # 노출 기록이 실패하면(파일 잠김 등) 검색은 되고 실패는 건강 신호로 남는다
        with mock.patch.object(search, "active_cards", lambda: [card]), \
                mock.patch.object(search, "select_llm", lambda q, c, k: None), \
                mock.patch("vmc_harness.ledger.exposure.record", side_effect=PermissionError("locked")):
            out = search.search("UI 열림 방향", root=self.root)
        self.assertEqual([c["id"] for c in out], [card["id"]])
        self.assertEqual(health.load(self.root)["last_error"]["stage"], "card_exposure")


class WindowsFlagsTest(GatesCase):
    def test_detached_uses_no_window_not_detached_and_retries_without_breakaway(self):
        from vmc_harness.core import proc
        calls = []

        def fake_popen(args, **kw):
            calls.append(kw.get("creationflags"))
            if len(calls) == 1:
                raise PermissionError(5, "Access is denied")   # job이 이탈을 허용하지 않을 때
            return mock.Mock(poll=lambda: 0)

        with mock.patch.object(proc.sys, "platform", "win32"), mock.patch.object(proc.subprocess, "Popen", fake_popen):
            proc.spawn_detached(["python", "-m", "vmc_harness", "judge", "--background"])
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0] & proc.CREATE_NO_WINDOW and calls[0] & proc.CREATE_BREAKAWAY_FROM_JOB)
        self.assertFalse(calls[0] & 0x8)      # DETACHED_PROCESS와 같이 주면 CREATE_NO_WINDOW가 무시된다(창 깜빡임)
        self.assertFalse(calls[1] & proc.CREATE_BREAKAWAY_FROM_JOB)
        self.assertTrue(calls[1] & proc.CREATE_NO_WINDOW and calls[1] & proc.CREATE_NEW_PROCESS_GROUP)

    def test_run_hides_console_on_windows(self):
        from vmc_harness.core import proc
        seen = {}

        def fake_run(args, **kw):
            seen.update(kw)
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(proc.sys, "platform", "win32"), mock.patch.object(proc.subprocess, "run", fake_run):
            proc.run(["git", "status"])
        self.assertEqual(seen.get("creationflags"), proc.CREATE_NO_WINDOW)
        if sys.platform != "win32":
            self.assertEqual(proc.no_window(), {})   # 비-Windows는 플래그 없음
        else:
            self.assertEqual(proc.no_window(), {"creationflags": proc.CREATE_NO_WINDOW})
