import json
import os
import re
from unittest import mock

from tests.util import HarnessTestCase

from vmc_harness.core import ledger
from vmc_harness.core.jsonio import write_json
from vmc_harness.hooks.contract import HookContext
from vmc_harness.core import clock
from vmc_harness.task import declare as dec, handlers, lifecycle, pm, state as st

NUM = re.compile(r"(P30|P50|P80|예산|토큰|\d+\s*%)")


def ctx(root, event, sid="S1", **kw):
    c = HookContext(event=event, agent="claude", raw={}, root=str(root), session_id=sid, cwd=str(root))
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def raw(**kw):
    d = {"intent": "정렬 버그 수정", "scope": ["src/list.py"],
         "predict": {"tokens": [1000, 2000, 4000], "agent_sec": [60, 120, 170]}}
    d.update(kw)
    return d


class HandlersTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        for k in st.SESSION_ENV:
            os.environ.pop(k, None)
        self.write("src/list.py", "def sort_items(xs):\n    return sorted(xs)\n")
        self.write("src/other.py", "y = 1\n")
        self.commit()
        sp = mock.patch("vmc_harness.task.lifecycle.spawn_measure")
        self.spawn = sp.start()
        self.addCleanup(sp.stop)
        self.addCleanup(clock.set_fake_now, None)
        handlers.on_session_start(ctx(self.root, "SessionStart"))
        handlers.on_user_prompt(ctx(self.root, "UserPromptSubmit", prompt="정렬 고쳐줘"))

    def _edit(self, rel, text=None):
        r = handlers.on_pre_tool(ctx(self.root, "PreToolUse", tool_name="Edit", write_paths=[rel]))
        if text is not None and not (r and r.block):
            self.write(rel, text)
        return r

    def test_block_without_declare(self):
        r = self._edit("src/list.py")
        self.assertTrue(r.block)
        self.assertTrue(r.reason.startswith("[vmc-harness:gate_violation] 착수 선언 없음"))
        self.assertIn("--intent", r.reason)
        self.assertIn("--scope", r.reason)
        # 읽기 도구는 막지 않는다
        self.assertIsNone(handlers.on_pre_tool(ctx(self.root, "PreToolUse", tool_name="Read")))
        # 프로젝트 밖 경로만 쓰는 도구는 선언 대상 아님
        self.assertIsNone(handlers.on_pre_tool(ctx(self.root, "PreToolUse", tool_name="Write",
                                                   write_paths=["/tmp/x.txt"])))

    def test_small_task_flow_and_ledger(self):
        import datetime as _dt
        t0 = _dt.datetime(2026, 10, 22, 19, 0, 0, tzinfo=_dt.timezone.utc)
        clock.set_fake_now(t0)
        dec.declare(self.root, raw())
        rows = ledger.read_tasks(self.root, include_open=True)
        self.assertEqual([r["status"] for r in rows], ["open"])   # 착수 선언 때 바로 "진행 중"으로
        self.assertIsNone(self._edit("src/list.py", "def sort_items(xs):\n    return sorted(xs, reverse=True)\n"))
        t = st.current(self.root, "S1")
        self.assertEqual(t["edits"], ["src/list.py"])
        self.assertTrue(t["first_edit_at"])
        self.assertIsNone(handlers.on_stop(ctx(self.root, "Stop")))
        tp = self._tmp / "t.jsonl"
        tp.write_text("\n".join(json.dumps(x) for x in [
            {"type": "user", "sessionId": "S1", "timestamp": "2026-10-22T19:00:00Z", "message": {"content": "정렬 고쳐줘"}},
            {"type": "assistant", "sessionId": "S1", "timestamp": "2026-10-22T19:01:00Z",
             "message": {"id": "m", "usage": {"input_tokens": 5, "output_tokens": 5}}}]) + "\n", encoding="utf-8")
        clock.set_fake_now(t0 + _dt.timedelta(minutes=2))
        handlers.on_session_end(ctx(self.root, "SessionEnd", transcript_path=str(tp)))
        self.spawn.assert_called_once()           # 무거운 측정은 백그라운드
        r = ledger.read_tasks(self.root)[0]
        self.assertEqual((r["status"], r["end_reason"]), ("ended", "session_end"))
        self.assertFalse(r["actual"]["measured"])  # 마감 표시만
        lifecycle.measure_pending(self.root)
        rows = ledger.read_tasks(self.root)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        for k in ("task_id", "session_id", "agent", "pc_id", "started_at", "ended_at", "declare", "size", "pm",
                  "actual", "overdesign", "out_of_scope", "evidence", "harness_overhead", "kernel_version"):
            self.assertIn(k, r)
        self.assertEqual(r["actual"], {"tokens": 10, "tokens_detail": {"input": 5, "output": 5, "cache_read": 0,
                                                                       "cache_creation": 0},
                                       "agent_sec": 60.0, "tool_calls": 0, "human_turns": 1, "measured": True})
        self.assertEqual(r["size"], "small")
        self.assertFalse(r["pm"]["called"])
        self.assertGreater(r["harness_overhead"]["sec"], 0)
        self.assertIsNone(st.current(self.root, "S1"))
        m = ledger.metrics_path(self.root, r["ended_at"][:10])
        self.assertIn("task", json.loads(m.read_text(encoding="utf-8")))

    def test_unmeasured_session_has_no_numbers(self):
        dec.declare(self.root, raw())
        handlers.on_session_end(ctx(self.root, "SessionEnd", transcript_path=str(self._tmp / "nope.jsonl")))
        lifecycle.measure_pending(self.root)
        r = ledger.read_tasks(self.root)[0]
        self.assertFalse(r["actual"]["measured"])
        self.assertIsNone(r["actual"]["agent_sec"])

    def _mid(self, opinion="trim", wish="정렬 옵션 부분은 10분 안에 끝나길 희망", cards=None):
        with mock.patch("vmc_harness.task.pm.run_pm",
                        return_value={"opinion": opinion, "wish": wish, "tokens": 30}), \
                mock.patch("vmc_harness.task.pm.run_cards", return_value=cards or []), \
                mock.patch("vmc_harness.task.pm.is_sync", return_value=True):
            return dec.declare(self.root, raw(scope=["src/list.py", "src/util.py"],
                                              predict={"tokens": [1, 2, 3], "agent_sec": [600, 900, 1500]}))

    def test_pm_opinion_once_on_first_out_of_scope_edit(self):
        self._mid()
        r = self._edit("src/list.py", "a = 1\n")  # 범위 안 — PM 안 기다림
        self.assertIsNone(r)
        r = self._edit("src/other.py", "y = 2\n")  # 범위 밖 첫 편집
        self.assertEqual(len(r.inject), 1)
        self.assertTrue(r.inject[0].startswith("[vmc-harness:pm_opinion]"))
        self.assertIn("10분 안에", r.inject[0])
        self.assertIsNone(self._edit("src/third.py", "z = 1\n"))  # 두 번째 범위 밖 — 다시 안 함
        self.assertEqual(st.current(self.root, "S1")["overhead"]["tokens"], 30)

    def test_pm_approve_is_silent(self):
        self._mid(opinion="approve", wish=None)
        self.assertIsNone(self._edit("src/other.py", "y = 2\n"))

    def test_pm_async_waits_up_to_limit(self):
        self._mid()
        t = st.current(self.root, "S1")
        write_json(pm.bg_path(self.root, t["task_id"]), {"status": "running"})
        with mock.patch("vmc_harness.core.config.get", side_effect=lambda k, root=None, profile=None:
                        0.3 if k == "pm.wait_s" else __import__("vmc_harness.core.config", fromlist=["x"]).defaults()[0][k]):
            r = self._edit("src/other.py", "y = 2\n")
        self.assertIsNone(r)  # 상한 안에 결과가 없으면 조용히 진행

    def test_cards_injected_once_before_first_edit(self):
        self._mid(opinion="approve", wish=None, cards=None)
        t = st.current(self.root, "S1")
        self.assertFalse(t["cards_delivered"])  # 동기 모드에선 카드가 없었음
        write_json(pm.bg_path(self.root, t["task_id"]), {"status": "done", "pm": {"called": True, "opinion": "approve"},
                                                         "cards": [{"id": "c1", "title": "한 페이지 먼저",
                                                                    "judgment": "먼저 한 곳에 적용"}]})
        r = handlers.on_post_tool(ctx(self.root, "PostToolUse", tool_name="Read"))
        self.assertTrue(r.inject[0].startswith("[vmc-harness:declare_context]"))
        self.assertIsNone(handlers.on_post_tool(ctx(self.root, "PostToolUse", tool_name="Read")))

    def test_overdesign_review_once_and_trimmed(self):
        dec.declare(self.root, raw())
        self._edit("src/list.py", "def sort_items(xs):\n    return sorted(xs)\n")
        self._edit("src/extra_helper.py", "def unused_helper(a):\n    return a\n" + "x = 1\n" * 10)
        r = handlers.on_stop(ctx(self.root, "Stop"))
        self.assertTrue(r.block)
        self.assertTrue(r.reason.startswith("[vmc-harness:gate_violation] 과설계 신호:"))
        self.assertIn("src/extra_helper.py", r.reason)
        self.assertIsNone(NUM.search(r.reason))
        # 줄여서 다시 종료 → 막지 않고 trimmed 기록
        os.remove(str(self.root / "src/extra_helper.py"))
        r2 = handlers.on_stop(ctx(self.root, "Stop"))
        self.assertTrue(r2 is None or not r2.block)
        t = st.current(self.root, "S1")
        self.assertTrue(t["overdesign"]["trimmed"])
        handlers.on_session_end(ctx(self.root, "SessionEnd"))
        lifecycle.measure_pending(self.root)
        rec = ledger.read_tasks(self.root)[0]
        self.assertIn("new_file_out_of_scope", rec["overdesign"]["signals"])
        self.assertTrue(rec["overdesign"]["trimmed"])

    def test_new_file_alone_does_not_block(self):
        """선언에 없던 새 파일·모듈만이면 막지 않고 완료 보고(범위 밖 변경)로만 — 실측 8건 모두 재선언만 했다(11-11~16)."""
        dec.declare(self.root, raw())
        self._edit("src/list.py", "def sort_items(xs):\n    return sorted(xs, key=str)\n")
        self._edit("docs/new_note.md", "메모\n")
        r = handlers.on_stop(ctx(self.root, "Stop"))
        self.assertFalse(r and r.block)
        self.assertIn("범위 밖 변경 1건", r.notice)
        self.assertFalse(st.current(self.root, "S1")["overdesign"].get("reviewed"))

    def test_empty_llm_signal_does_not_block(self):
        """지적 내용이 빈 LLM 신호로 '요청에 없는 옵션·기능 ' 빈 차단을 내지 않는다(2026-11-14 현장)."""
        dec.declare(self.root, raw())
        self._edit("src/list.py", "def sort_items(xs):\n    return sorted(xs, key=str)\n")
        t = st.current(self.root, "S1")
        t["size"] = "mid"
        st.save(self.root, t)
        with mock.patch("vmc_harness.task.overdesign.spawn_llm"), \
                mock.patch("vmc_harness.task.overdesign.read_llm",
                           return_value={"status": "done", "signals": [{"kind": "unrequested_option", "name": " "}]}):
            r = handlers.on_stop(ctx(self.root, "Stop"))
        self.assertFalse(r and r.block)

    def test_out_of_scope_record_only(self):
        dec.declare(self.root, raw())
        self._edit("src/list.py", "a = 2\n")
        self._edit("src/other.py", "y = 3\n")
        r = handlers.on_stop(ctx(self.root, "Stop"))
        self.assertFalse(r.block)
        self.assertEqual(r.notice, "범위 밖 변경 1건")
        handlers.on_session_end(ctx(self.root, "SessionEnd"))
        rec = ledger.read_tasks(self.root)[0]
        self.assertEqual(rec["out_of_scope"], [{"path": "src/other.py", "reason": None}])
        self.assertEqual(ledger.list_findings(self.root), [])  # 요청 밖 변경은 finding이 아니다

    def test_stale_task_finalized_on_next_session_start(self):
        st.session_touch(self.root, "OLD", "codex")
        dec.declare(self.root, raw(), session_id="OLD", agent="codex")
        t = st.current(self.root, "OLD")
        old = "2020-01-01T00:00:00+00:00"
        for k in ("started_at", "declared_at", "last_activity_at"):
            t[k] = old
        t["segments"] = [{"start": old, "end": None}]
        st.save(self.root, t)
        self.spawn.reset_mock()
        handlers.on_session_start(ctx(self.root, "SessionStart", sid="NEW"))
        rows = ledger.read_tasks(self.root)
        self.assertEqual([r["session_id"] for r in rows], ["OLD"])
        self.assertEqual((rows[0]["end_reason"], rows[0]["ended_at"]), ("idle", old))  # 끝난 시각 = 마지막 활동
        self.spawn.assert_called_once()  # 측정은 SessionStart 동기 경로가 아니라 백그라운드


class DispatcherIntegrationTest(HarnessTestCase):
    """gates 디스패처를 거쳐도 막는 사유·PM 의견이 허용 목록 검사를 통과하는가(task 핸들러만 연결)."""

    def setUp(self):
        super().setUp()
        for k in st.SESSION_ENV:
            os.environ.pop(k, None)
        self.write("src/list.py", "a = 1\n")
        self.commit()

    def _dispatch(self, event, **raw_kw):
        try:
            from vmc_harness.hooks import dispatcher
        except ImportError:
            self.skipTest("gates 디스패처 없음")
        raw_in = dict({"session_id": "S1", "cwd": str(self.root), "hook_event_name": event}, **raw_kw)
        return dispatcher.dispatch(event, "claude", json.dumps(raw_in), modules=[("vmc_harness.task.handlers", False)])

    def test_no_declare_reason_survives_filter(self):
        out = self._dispatch("PreToolUse", tool_name="Edit",
                             tool_input={"file_path": str(self.root / "src/list.py"), "old_string": "a", "new_string": "b"})
        text = out[0] if isinstance(out, tuple) else out
        self.assertIn("착수 선언 없음", text)
        self.assertNotIn("하네스 게이트가 막음", text)
