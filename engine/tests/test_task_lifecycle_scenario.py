"""작업 수명주기·"이번 작업이 바꾼 것" 시나리오 (현장 결함 ①③ C1 C2 C5 C7 D1 D2 D3 회귀).

가짜 시계(core.clock.set_fake_now)로 며칠치를 압축한다: 여러 세션 · 비정상 종료(SessionEnd 없음) · resume · 재선언 ·
하루 넘김 · 세션 없는 작업 · 밤 루프. 훅은 디스패처(gates + task 핸들러)를 거친다. 합성 데이터만.
"""

import datetime as _dt
import json
import os
import subprocess
from unittest import mock

from tests.util import HarnessTestCase

from vmc_harness.core import clock, ledger
from vmc_harness.hooks import dispatcher
from vmc_harness.task import declare as dec, lifecycle, scope, state as st

KST = _dt.timezone(_dt.timedelta(hours=9))
MODULES = [("vmc_harness.gates.handlers", True), ("vmc_harness.task.handlers", False)]


def at(day, hh, mm=0):
    return _dt.datetime(2026, 10, 27 + day, hh, mm, tzinfo=KST)


def raw(intent, scope_, test_files=None, **kw):
    d = {"intent": intent, "scope": scope_, "predict": {"tokens": [500, 1000, 2000], "agent_sec": [30, 60, 120]}}
    if test_files:
        d["test_change"] = {"files": test_files, "reason": "기대값이 바뀐 요구사항"}
    d.update(kw)
    return d


def usage(i, o, cr=0, cc=0):
    return {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cc}


class LifecycleScenarioTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        for k in st.SESSION_ENV:
            os.environ.pop(k, None)
        self.addCleanup(clock.set_fake_now, None)
        sp = mock.patch("vmc_harness.task.lifecycle.spawn_measure")
        self.spawn = sp.start()
        self.addCleanup(sp.stop)
        self.write("src/a.py", "def a():\n    return 1\n")
        self.write("tests/test_a.py", "def test_a():\n    assert True\n")
        self.write("tests/test_b.py", "def test_b():\n    assert True\n")
        self.write("docs/guide.md", "# guide\n")
        self.commit()
        self.transcripts = {}

    # ---- 도우미 ----

    def clock(self, when):
        clock.set_fake_now(when)

    def tp(self, sid):
        return self._tmp / ("%s.jsonl" % sid)

    def say(self, sid, when, kind, **kw):
        """합성 Claude 세션 기록 한 줄."""
        rows = self.transcripts.setdefault(sid, [])
        ts = when.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
        if kind == "user":
            rows.append({"type": "user", "sessionId": sid, "timestamp": ts, "message": {"content": kw["text"]}})
        else:
            rows.append({"type": "assistant", "sessionId": sid, "timestamp": ts,
                         "message": {"id": "m%d" % len(rows), "usage": kw["usage"]}})
        self.tp(sid).write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def hook(self, event, sid, **kw):
        payload = dict({"session_id": sid, "cwd": str(self.root), "transcript_path": str(self.tp(sid))}, **kw)
        out, _ = dispatcher.dispatch(event, "claude", json.dumps(payload), modules=MODULES, cwd=str(self.root))
        return json.loads(out) if out.strip() else {}

    def edit(self, sid, rel, text, tool="Edit"):
        out = self.hook("PreToolUse", sid, tool_name=tool,
                        tool_input={"file_path": str(self.root / rel), "old_string": "x", "new_string": "y",
                                    "content": text})
        hs = out.get("hookSpecificOutput") or {}
        blocked = hs.get("permissionDecision") == "deny" or out.get("decision") == "block"
        if not blocked:
            self.write(rel, text)
            self.hook("PostToolUse", sid, tool_name=tool, tool_input={"file_path": str(self.root / rel)})
        return blocked

    def rec(self, task_id, include_open=True, now=None):
        rows = [r for r in ledger.read_tasks(self.root, include_open=include_open, at=now) if r["task_id"] == task_id]
        return rows[0] if rows else None

    # ---- 시나리오 ----

    def test_days_of_sessions(self):
        # D1 09:00 세션 S1 — 작업 A 선언(테스트 수정 허용 test_a)
        self.clock(at(0, 9, 0))
        self.hook("SessionStart", "S1", source="startup")
        self.say("S1", at(0, 9, 0), "user", text="a 고쳐줘")
        self.hook("UserPromptSubmit", "S1", prompt="a 고쳐줘")
        self.clock(at(0, 9, 1))
        a = dec.declare(self.root, raw("a 반환값 수정", ["src/a.py"], ["tests/test_a.py"]), session_id="S1")["task"]
        # D1: 착수 선언 때 장부에 "진행 중"으로 바로 남는다
        self.assertEqual(self.rec(a["task_id"])["status"], "open")
        self.clock(at(0, 9, 5))
        self.assertFalse(self.edit("S1", "src/a.py", "def a():\n    return 2\n"))
        self.say("S1", at(0, 9, 10), "assistant", usage=usage(100, 50, cr=100000, cc=1000))
        self.clock(at(0, 9, 6))
        self.hook("Stop", "S1", stop_hook_active=False)
        self.assertEqual(self.rec(a["task_id"])["last_activity_at"], at(0, 9, 6).isoformat(timespec="seconds"))

        # C1: 같은 작업 재선언(범위 겹침) — 테스트 수정 허용 파일이 누적된다
        self.clock(at(0, 9, 10))
        r = dec.declare(self.root, raw("a 반환값 수정", ["src/a.py"], ["tests/test_b.py"]), session_id="S1")
        self.assertFalse(r["created"])
        self.assertEqual(r["task"]["task_id"], a["task_id"])
        self.assertEqual(r["task"]["declare"]["test_change"]["files"], ["tests/test_a.py", "tests/test_b.py"])
        self.clock(at(0, 9, 12))
        self.assertFalse(self.edit("S1", "tests/test_a.py", "def test_a():\n    assert 2\n"), "C1: 앞서 허용한 테스트가 막힘")
        self.assertFalse(self.edit("S1", "tests/test_b.py", "def test_b():\n    assert 2\n"))
        self.clock(at(0, 9, 13))
        self.hook("Stop", "S1", stop_hook_active=False)

        # 같은 세션 새 작업 B(범위 안 겹침) → A는 "새 선언"으로 마감
        self.clock(at(0, 9, 20))
        r = dec.declare(self.root, raw("가이드 문서 갱신", ["docs/guide.md"]), session_id="S1")
        b = r["task"]
        self.assertTrue(r["created"])
        self.assertEqual(r["closed"]["task_id"], a["task_id"])
        ra = self.rec(a["task_id"])
        self.assertEqual((ra["status"], ra["end_reason"], ra["ended_at"]),
                         ("ended", "redeclare", at(0, 9, 20).isoformat(timespec="seconds")))
        self.assertEqual(sorted(o["path"] for o in ra["out_of_scope"]), [])  # 테스트 수정은 선언 범위 안
        self.assertEqual(self.spawn.call_args.args[1], a["task_id"])
        # A가 만든 변경은 B의 것이 아니다(착수 스냅샷)
        self.clock(at(0, 9, 25))
        self.assertFalse(self.edit("S1", "docs/guide.md", "# guide v2\n"))
        self.say("S1", at(0, 9, 25), "assistant", usage=usage(10, 20, cr=50000, cc=500))
        self.clock(at(0, 9, 30))
        self.hook("Stop", "S1", stop_hook_active=False)
        self.assertEqual(st.by_id(self.root, b["task_id"])["scope_cache"]["files"], ["docs/guide.md"])

        # 비정상 종료: SessionEnd 없음. 11:00 — 아직 조용한 시간 상한(120분) 전이면 진행 중
        self.assertEqual(self.rec(b["task_id"], now=at(0, 11, 0))["status"], "open")
        # 장부를 읽는 쪽은 상한이 지나면 마지막 활동 시각에 끝난 것으로 계산한다(쓰지 않아도)
        eff = self.rec(b["task_id"], now=at(0, 12, 0))
        self.assertEqual((eff["status"], eff["end_reason"], eff["ended_at"]),
                         ("ended", "idle", at(0, 9, 30).isoformat(timespec="seconds")))

        # D1 12:40 다른 세션 S2 시작 → B 마감 표시(측정은 백그라운드)
        self.clock(at(0, 12, 40))
        self.spawn.reset_mock()
        self.hook("SessionStart", "S2", source="startup")
        tb = st.by_id(self.root, b["task_id"])
        self.assertEqual((tb["end_reason"], tb["ended_at"]), ("idle", at(0, 9, 30).isoformat(timespec="seconds")))
        self.assertTrue(tb["measure_pending"])
        self.spawn.assert_called_once()
        # 백그라운드 측정(여기선 직접): 작업 구간만, 캐시 읽기 제외(D2)
        lifecycle.measure_pending(self.root)
        ra, rb = self.rec(a["task_id"]), self.rec(b["task_id"])
        self.assertEqual(ra["actual"]["tokens"], 100 + 1000 + 50)
        self.assertEqual(ra["actual"]["tokens_detail"], {"input": 100, "output": 50, "cache_read": 100000,
                                                         "cache_creation": 1000})
        self.assertEqual(ra["actual"]["agent_sec"], 19 * 60.0)        # 턴 09:00~09:25 ∩ A 구간 09:01~09:20
        self.assertEqual(rb["actual"]["tokens"], 10 + 500 + 20)
        self.assertEqual(rb["actual"]["agent_sec"], 5 * 60.0)          # 09:20~09:25
        self.assertEqual(ra["actual"]["human_turns"], 0)               # 사람 메시지(09:00)는 선언 전

        # S2: 작업 C — 새 테스트 파일을 만들고 커밋한 뒤 다시 고쳐도 "기존 검증물 변조"가 아니다(C2)
        self.clock(at(0, 12, 41))
        c = dec.declare(self.root, raw("새 테스트 추가", ["tests/test_new.py"]), session_id="S2")["task"]
        self.assertFalse(self.edit("S2", "tests/test_new.py", "def test_new():\n    assert 1\n", tool="Write"))
        subprocess.run(["git", "add", "tests/test_new.py"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "new test"], cwd=self.root, check=True)
        self.clock(at(0, 12, 45))
        self.assertFalse(self.edit("S2", "tests/test_new.py", "def test_new():\n    assert 2\n"), "C2: 새로 만든 파일이 막힘")
        self.assertTrue(self.edit("S2", "tests/test_a.py", "def test_a():\n    assert 3\n"), "기존 테스트는 여전히 막혀야 함")
        self.clock(at(0, 12, 46))
        self.hook("Stop", "S2", stop_hook_active=False)

        # D2(하루 넘김) 10:00 — S1 resume 후 도구 활동 → 마감된 B를 다시 열어 이어 붙인다
        self.clock(at(1, 10, 0))
        self.hook("SessionStart", "S1", source="resume")
        self.say("S1", at(1, 10, 0), "user", text="가이드 하나 더")
        self.clock(at(1, 10, 2))
        self.assertFalse(self.edit("S1", "docs/guide.md", "# guide v3\n"))
        tb = st.by_id(self.root, b["task_id"])
        self.assertIsNone(tb["ended_at"])
        self.assertEqual(len(tb["segments"]), 2)
        self.assertEqual(self.rec(b["task_id"])["status"], "open")
        self.say("S1", at(1, 10, 5), "assistant", usage=usage(1, 2, cr=9, cc=3))
        self.clock(at(1, 10, 6))
        self.hook("Stop", "S1", stop_hook_active=False)
        self.clock(at(1, 10, 10))
        self.hook("SessionEnd", "S1", reason="exit")
        rb = self.rec(b["task_id"])
        self.assertEqual((rb["status"], rb["end_reason"]), ("ended", "session_end"))
        lifecycle.measure_pending(self.root)
        rb = self.rec(b["task_id"])
        self.assertEqual(rb["actual"]["tokens"], (10 + 500 + 20) + (1 + 3 + 2))   # 두 구간 합
        # D1 09:20~09:25 + D2 다시 연 시각(첫 도구 활동 10:02)~10:05 — 조용했던 하루는 세지 않는다
        self.assertEqual(rb["actual"]["agent_sec"], 5 * 60.0 + 3 * 60.0)
        self.assertTrue(ledger.metrics_path(self.root, "2026-10-28").exists())    # 끝난 날(D2) 집계

        # S2의 C는 SessionEnd 없이 방치 — brief(진행 중 목록)는 더 이상 진행 중으로 보지 않는다
        self.assertNotIn(c["task_id"], [t["task_id"] for t in st.open_tasks(self.root)])
        # 밤 루프가 마감·측정을 끝낸다
        lifecycle.nightly(self.root)
        rc = self.rec(c["task_id"], include_open=False)
        self.assertEqual((rc["end_reason"], rc["ended_at"]), ("idle", at(0, 12, 46).isoformat(timespec="seconds")))
        self.assertIsNotNone(rc["measured_at"])
        # 장부에서 작업이 하나도 빠지지 않는다(D1)
        self.assertEqual(sorted(r["task_id"] for r in ledger.read_tasks(self.root)),
                         sorted([a["task_id"], b["task_id"], c["task_id"]]))

    def test_task_without_session(self):
        # 훅 세션이 하나도 없을 때(터미널에서 직접 선언) — 세션 없는 작업도 같은 기준으로 끝난다
        self.clock(at(0, 9, 0))
        t = dec.declare(self.root, raw("a 수정", ["src/a.py"]))["task"]
        self.assertIsNone(t["session_id"])
        self.assertEqual(self.rec(t["task_id"])["status"], "open")
        self.clock(at(0, 13, 0))
        self.assertIsNone(st.current(self.root, None))
        self.assertEqual(self.rec(t["task_id"], include_open=False)["end_reason"], "idle")
        closed = lifecycle.sweep(self.root)
        self.assertEqual([x["task_id"] for x in closed], [t["task_id"]])

    def test_same_session_new_session_resume_after_session_end_same_day(self):
        # SessionEnd 뒤 같은 세션을 다시 열어 바로 이어 쓰면 같은 작업에 붙는다(새 작업 선언 없이)
        self.clock(at(0, 9, 0))
        self.hook("SessionStart", "S9", source="startup")
        t = dec.declare(self.root, raw("a 수정", ["src/a.py"]), session_id="S9")["task"]
        self.assertFalse(self.edit("S9", "src/a.py", "def a():\n    return 5\n"))
        self.clock(at(0, 9, 5))
        self.hook("SessionEnd", "S9", reason="exit")
        self.clock(at(0, 9, 7))
        self.hook("SessionStart", "S9", source="resume")
        self.assertFalse(self.edit("S9", "src/a.py", "def a():\n    return 6\n"))
        self.assertEqual(st.current(self.root, "S9")["task_id"], t["task_id"])
        self.assertEqual(st.by_id(self.root, t["task_id"])["reopened"], 1)

    def test_new_task_flag_and_explicit_same(self):
        self.clock(at(0, 9, 0))
        self.hook("SessionStart", "S3", source="startup")
        t1 = dec.declare(self.root, raw("a 수정", ["src/a.py"]), session_id="S3")["task"]
        r = dec.declare(self.root, raw("a 수정 2", ["src/a.py"]), session_id="S3", mode="new")
        self.assertTrue(r["created"])
        self.assertEqual(r["closed"]["task_id"], t1["task_id"])
        t2 = r["task"]
        self.assertFalse(self.edit("S3", "src/a.py", "def a():\n    return 7\n"))
        r = dec.declare(self.root, raw("문서", ["docs/guide.md"]), session_id="S3", mode="same")
        self.assertEqual(r["task"]["task_id"], t2["task_id"])


class SnapshotTest(HarnessTestCase):
    """착수 스냅샷 하나를 모든 게이트가 쓴다 — 하네스가 쓴 파일·착수 전 변경 제외(C7, D3)."""

    def setUp(self):
        super().setUp()
        for k in st.SESSION_ENV:
            os.environ.pop(k, None)
        sp = mock.patch("vmc_harness.task.lifecycle.spawn_measure")
        sp.start()
        self.addCleanup(sp.stop)
        (self.root / "AGENTS.md").write_bytes(b"# team rules\r\n- keep it small\r\n")
        self.write("CLAUDE.md", "# claude\n")
        self.write("fe/src/components/NavTabs.tsx", "export const T = 1;\n")
        self.write("src/x.py", "x = 1\n")
        self.commit()

    def test_harness_files_and_pre_existing_changes_are_not_this_task(self):
        from vmc_harness.gates.rules import BEGIN, END
        # 세션 전: 하네스가 init에서 쓴 것(규칙 블록 · import 줄 · .mcp.json · settings) + 사람의 미커밋 변경
        (self.root / "AGENTS.md").write_bytes(
            b"# team rules\r\n- keep it small\r\n\r\n" + BEGIN.encode() + b"\r\n- rule\r\n" + END.encode() + b"\r\n")
        self.write("CLAUDE.md", "# claude\n@AGENTS.md\n")
        self.write(".mcp.json", "{\"mcpServers\": {\"vmc-brain\": {}}}\n")
        self.write(".claude/settings.json", "{}\n")
        self.write("fe/src/components/NavTabs.tsx", "export const T = 2;\n")
        st.session_touch(self.root, "S1", "claude")
        t = dec.declare(self.root, raw("x 수정", ["src/x.py"]), session_id="S1")["task"]
        self.assertEqual(scope.touched(self.root, t), [])
        self.agent_write("src/x.py", "x = 2\n")
        t = st.by_id(self.root, t["task_id"])
        self.assertEqual(scope.touched(self.root, t), ["src/x.py"])
        self.assertEqual(scope.touch_ratio(self.root, t)["in_scope"], 1.0)
        # 하네스가 블록만 다시 렌더해도(줄끝 그대로) 이번 작업이 아니다
        (self.root / "AGENTS.md").write_bytes(
            b"# team rules\r\n- keep it small\r\n\r\n" + BEGIN.encode() + b"\r\n- rule 2\r\n" + END.encode() + b"\r\n")
        self.assertEqual(scope.touched(self.root, t), ["src/x.py"])
        # 사람이 작업 중 미리 바꿔 둔 파일을 다시 고치면 그건 이번 작업이 바꾼 것
        self.agent_write("fe/src/components/NavTabs.tsx", "export const T = 3;\n")
        t = st.by_id(self.root, t["task_id"])
        self.assertEqual(scope.touched(self.root, t), ["fe/src/components/NavTabs.tsx", "src/x.py"])
        # AGENTS.md의 사람 영역을 고치면 이번 작업
        (self.root / "AGENTS.md").write_bytes(b"# team rules v2\r\n")
        scope.record_writes(t, ["AGENTS.md"])
        self.assertIn("AGENTS.md", scope.touched(self.root, t))

    def test_korean_path_not_escaped_and_garbage_edit_paths_dropped(self):
        st.session_touch(self.root, "S1", "claude")
        t = dec.declare(self.root, raw("문서", ["docs/"]), session_id="S1")["task"]
        self.write("docs/보고서.md", "# 보고서\n")
        self.write("src/x.py", "x = 9\n")
        t = st.by_id(self.root, t["task_id"])
        t["edits"] = ["!before.activities.some", "!gone.includes", "docs/보고서.md", "src/x.py"]   # 셸 명령 조각이 섞인 기록
        st.save(self.root, t)
        files = scope.touched(self.root, t)
        self.assertEqual(files, ["docs/보고서.md", "src/x.py"])
        self.assertEqual([o["path"] for o in scope.out_of_scope(self.root, t, files)], ["src/x.py"])
        self.assertEqual(scope.numstat(self.root, t, ["src/x.py"])["src/x.py"], (1, 1))

    def test_resume_absorbs_outside_changes(self):
        st.session_touch(self.root, "S1", "claude")
        t = dec.declare(self.root, raw("x 수정", ["src/x.py"]), session_id="S1")["task"]
        # 세션 사이에 사람이 다른 파일을 고침 → 같은 세션 resume
        self.write("fe/src/components/NavTabs.tsx", "export const T = 5;\n")
        from vmc_harness.hooks.contract import HookContext
        from vmc_harness.task import handlers
        handlers.on_session_start(HookContext("SessionStart", "claude", {}, root=str(self.root), session_id="S1"))
        self.assertEqual(scope.touched(self.root, st.by_id(self.root, t["task_id"])), [])


class GatesShareSnapshotTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        for k in st.SESSION_ENV:
            os.environ.pop(k, None)
        sp = mock.patch("vmc_harness.task.lifecycle.spawn_measure")
        sp.start()
        self.addCleanup(sp.stop)
        self.addCleanup(clock.set_fake_now, None)
        self.write("fe/src/pages/Orders.tsx", "export default 1;\n")
        self.write("docs/note.md", "n\n")
        self.commit()
        st.session_touch(self.root, "S1", "claude")

    def test_completion_evidence_invalidated_only_by_behavior_edits(self):
        from vmc_harness.gates import completion, evidence
        clock.set_fake_now(at(0, 9, 0))
        t = dec.declare(self.root, raw("주문 페이지", ["fe/src/pages/Orders.tsx", "docs/note.md"]), session_id="S1")["task"]
        page = self.agent_write("fe/src/pages/Orders.tsx", "export default 2;\n")
        t = st.by_id(self.root, t["task_id"])
        os.utime(str(page), (at(0, 9, 1).timestamp(),) * 2)
        self.assertIsNotNone(completion.evaluate(str(self.root), "S1", t))
        clock.set_fake_now(at(0, 9, 2))
        shot = self._tmp / "shot.png"
        shot.write_bytes(b"\x89PNG fake")
        os.utime(str(shot), (at(0, 9, 2).timestamp(),) * 2)
        m1 = evidence.screenshot(str(self.root), t["task_id"], str(shot), "주문 목록")
        self.assertIsNone(completion.evaluate(str(self.root), "S1", t))
        # 문서 편집은 증거를 무효로 만들지 않는다(C5)
        doc = self.agent_write("docs/note.md", "n2\n")
        t = st.by_id(self.root, t["task_id"])
        os.utime(str(doc), (at(0, 9, 5).timestamp(),) * 2)
        self.assertIsNone(completion.evaluate(str(self.root), "S1", t))
        # 같은 스크린샷을 다시 등록해도 한 번만(C5)
        m2 = evidence.screenshot(str(self.root), t["task_id"], str(shot), "주문 목록")
        self.assertTrue(m2.get("duplicate"))
        self.assertEqual(m2["marker_id"], m1["marker_id"])
        self.assertEqual(len(evidence.list_markers(str(self.root), t["task_id"])), 1)
        # 동작 파일을 다시 고치면 증거가 다시 필요
        os.utime(str(page), (at(0, 9, 6).timestamp(),) * 2)
        self.assertIsNotNone(completion.evaluate(str(self.root), "S1", t))

    def test_proceed_picks_overdesign_block(self):
        from vmc_harness.gates import proceed, state as gstate
        from vmc_harness.hooks.contract import HookContext
        from vmc_harness.task import handlers
        clock.set_fake_now(at(0, 9, 0))
        pre = HookContext("PreToolUse", "claude", {}, root=str(self.root), session_id="S1", tool_name="Bash",
                          command="sqlcmd -Q 'DELETE FROM t'")
        gstate.record_block(str(self.root), pre, "db_direct", "DB 직접 접속 차단됨", gate_id="kernel:db_direct")
        dec.declare(self.root, raw("주문 페이지", ["fe/src/pages/Orders.tsx"]), session_id="S1")
        self.agent_write("src/extra_helper.py", "def unused_helper(a):\n    return a\n")
        clock.set_fake_now(at(0, 9, 3))
        stop = HookContext("Stop", "claude", {}, root=str(self.root), session_id="S1", cwd=str(self.root))
        r = handlers.on_stop(stop)
        self.assertTrue(r and r.block)
        sid, b = proceed.latest_block(str(self.root))
        self.assertEqual((sid, b["gate"]), ("S1", "overdesign"))
        f = proceed.from_cli(str(self.root), require_tty=False)
        self.assertEqual(f["target"]["gate_id"], "kernel:overdesign")


class CodexWindowTest(HarnessTestCase):
    def test_codex_tokens_are_window_delta_without_cache(self):
        from vmc_harness import sessions
        p = self._tmp / "rollout-x.jsonl"

        def tc(ts, inp, cached, out):
            return {"timestamp": ts, "type": "event_msg", "payload": {"type": "token_count", "info": {
                "total_token_usage": {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out,
                                      "total_tokens": inp + out}}}}
        rows = [{"timestamp": "2026-10-27T00:00:00Z", "type": "session_meta", "payload": {"id": "cx", "cwd": "/w"}},
                {"timestamp": "2026-10-27T00:00:00Z", "type": "event_msg",
                 "payload": {"type": "task_started", "turn_id": "t1"}},
                tc("2026-10-27T00:01:00Z", 1000, 800, 100),
                tc("2026-10-27T00:05:00Z", 5000, 4000, 300),
                {"timestamp": "2026-10-27T00:06:00Z", "type": "event_msg",
                 "payload": {"type": "task_complete", "turn_id": "t1"}}]
        p.write_text("\r\n".join(json.dumps(r) for r in rows) + "\r\n", encoding="utf-8")   # CRLF(Windows)
        win = [(_dt.datetime(2026, 10, 27, 0, 2, tzinfo=_dt.timezone.utc), None)]
        s = sessions.parse(str(p), "codex", windows=win)
        self.assertTrue(s.measured)
        self.assertEqual(s.token_parts, {"input": 800, "output": 200, "cache_read": 3200, "cache_creation": 0})
        self.assertEqual(s.work_tokens(), 1000)
        self.assertEqual(s.agent_sec, 240.0)
