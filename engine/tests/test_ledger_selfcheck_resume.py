"""자가 점검 — 이어 쓴 세션(--resume, 턴마다 SessionStart/SessionEnd)의 새 구간만 다시 본다 (0.1.12 현장 결함 회귀).

현장: 메시지마다 같은 세션을 --resume으로 이어 쓰는 환경에서 자가 점검이 첫 SessionEnd에만 돌아 세션당 사람 턴이 늘 1이었다.
합성 Claude 세션 기록만 쓴다(실제 파서로 센다 — LLM만 대역).
"""

import datetime as _dt
import json
import os
import time
from unittest import mock

from tests.util import HarnessTestCase

from vmc_harness.core import events
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.project import local_dir
from vmc_harness.ledger import metrics, selfcheck
from vmc_harness.sessions import claude_dir_name

UTC = _dt.timezone.utc
SID = "11111111-2222-3333-4444-555555555555"


def t(mm, ss=0):
    return _dt.datetime(2026, 11, 1, 10, mm, ss, tzinfo=UTC)


def z(d):
    return d.isoformat().replace("+00:00", "Z")


class ResumeSelfcheckTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.cdir = self._tmp / "claude" / claude_dir_name(self.root.resolve())
        self.cdir.mkdir(parents=True)
        os.environ["VMC_CLAUDE_ROOT"] = str(self._tmp / "claude")
        self.path = self.cdir / ("%s.jsonl" % SID)
        self.rows = []
        self.n = 0
        self.calls = []

    def _add(self, r):
        r.setdefault("sessionId", SID)
        r.setdefault("cwd", str(self.root))
        self.rows.append(r)
        self.path.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in self.rows) + "\n", encoding="utf-8")

    def human(self, when, text):
        self._add({"type": "user", "timestamp": z(when), "promptSource": "user", "message": {"content": text}})

    def reply(self, when, tool=True):
        self.n += 1
        content = [{"type": "text", "text": "ok"}]
        if tool:
            content.append({"type": "tool_use", "id": "tu%d" % self.n, "name": "Edit",
                            "input": {"file_path": str(self.root / "a.py")}})
        self._add({"type": "assistant", "timestamp": z(when),
                   "message": {"id": "m%d" % self.n, "content": content,
                               "usage": {"input_tokens": 10, "output_tokens": 5}}})

    def hook_feedback(self, when):
        # Stop 훅이 막으면 Claude Code가 넣는 메타 메시지 — 사람 턴이 아니다
        self._add({"type": "user", "timestamp": z(when), "isMeta": True,
                   "message": {"content": "Stop hook feedback: [vmc-harness:gate] 증거 없음"}})

    def notification(self, when):
        self._add({"type": "user", "timestamp": z(when), "origin": {"kind": "task-notification"},
                   "message": {"content": "<task-notification> done </task-notification>"}})

    def check(self, out=None, sid=SID):
        def fake(body, **kw):
            self.calls.append(body)
            return {"text": json.dumps(out or {"corrections": 0, "learnings": [], "explicit_requests": []}),
                    "tokens": 50, "sec": 0.1}
        with mock.patch("vmc_harness.core.llm.available", return_value=True), \
                mock.patch("vmc_harness.core.llm.claude_p", side_effect=fake):
            return selfcheck.run_session(self.root, str(self.path), "claude", sid)

    def test_each_resumed_turn_counted_once(self):
        # 턴 1
        self.human(t(0), "a.py의 반환값을 2로 바꿔줘")
        self.reply(t(1))
        r1 = self.check({"corrections": 5, "learnings": [], "explicit_requests": []})
        self.assertEqual((r1["human_turns"], r1["tool_calls"], r1["continued"]), (1, 1, False))
        self.assertEqual(r1["corrections"], 1)  # 넘긴 발화 수를 넘지 않는다
        # 턴 2(--resume): 훅 피드백·백그라운드 알림은 사람 턴이 아니다
        self.hook_feedback(t(2))
        self.notification(t(3))
        self.reply(t(3, 30))
        self.human(t(5), "아니 그게 아니라 3으로 바꾸라고 했잖아")
        self.reply(t(6))
        r2 = self.check({"corrections": 1, "learnings": [], "explicit_requests": []})
        self.assertEqual((r2["human_turns"], r2["continued"], r2["corrections"]), (1, True, 1))
        self.assertEqual(r2["tool_calls"], 2)  # 알림 턴 + 턴 2의 도구 호출(이미 본 턴 1은 빼고)
        # LLM에는 새 발화만 — 이미 본 턴 1을 다시 넘기지 않는다
        self.assertEqual(len(self.calls), 2)
        self.assertIn("3으로 바꾸라고", self.calls[1])
        self.assertNotIn("반환값을 2로", self.calls[1])
        self.assertNotIn("Stop hook feedback", self.calls[1])
        # 새 구간 없이 또 SessionEnd → 세지 않고 LLM도 안 부른다
        self.assertEqual(self.check()["skipped"], "새 구간 없음")
        self.assertEqual(len(self.calls), 2)
        # 승인만 한 턴('ㄱㄱ') — 세기만 하고 LLM은 부르지 않는다(selfcheck.min_llm_chars)
        self.human(t(8), "ㄱㄱ")
        self.reply(t(9))
        r3 = self.check()
        self.assertEqual((r3["human_turns"], len(self.calls)), (1, 2))
        evs = events.read(self.root, "session_selfcheck")
        self.assertEqual([e["human_turns"] for e in evs], [1, 1, 1])
        self.assertEqual(sum(e["corrections"] for e in evs), 2)
        # 일일 집계: 세션은 하나, 사람 턴은 셋
        m = list(metrics.aggregate(evs, pc="pc").values())[0]
        self.assertEqual((m["sessions"], m["human_turns"], m["corrections"], m["human_turns_per_session"]),
                         (1, 3, 2, 3.0))

    def test_legacy_done_session_continues_from_now(self):
        # 0.1.11 이하: 첫 턴만 보고 커서 없이 "점검함"만 남긴 세션
        self.human(t(0), "첫 지시입니다")
        self.reply(t(1))
        self.human(t(3), "두 번째 지시입니다")
        self.reply(t(4))
        write_json(local_dir(self.root) / "selfcheck-done.json", {"sessions": [SID]})
        r = self.check()
        self.assertIn("옛 점검", r["skipped"])
        self.assertEqual(self.calls, [])  # 옛 구간을 LLM에 다시 넘기지 않는다
        self.human(t(6), "세 번째 지시입니다")
        self.reply(t(7))
        r = self.check()
        self.assertEqual((r["human_turns"], r["continued"]), (1, True))
        self.assertIn("세 번째", self.calls[0])
        self.assertNotIn("두 번째", self.calls[0])

    def test_pending_picks_up_grown_session(self):
        self.human(t(0), "첫 지시입니다")
        self.reply(t(1))
        old = time.time() - 3600
        os.utime(self.path, (old, old))
        self.assertEqual([p.name for _, p in selfcheck.pending(self.root)], [self.path.name])
        self.check()
        self.assertEqual(selfcheck.pending(self.root), [])  # 그 뒤로 안 자랐다
        # SessionEnd 분리 실행이 실패한 채 기록이 자랐다 → 조용해진 뒤 밀린 점검이 새 구간만 잡는다
        self.human(t(5), "두 번째 지시입니다")
        self.reply(t(6))
        os.utime(self.path, (old + 60, old + 60))
        self.assertEqual(len(selfcheck.pending(self.root)), 1)
        out = selfcheck.run_pending(self.root)
        self.assertEqual([(x["human_turns"], x["continued"]) for x in out], [(1, True)])

    def test_concurrent_run_skipped(self):
        self.human(t(0), "첫 지시입니다")
        self.reply(t(1))
        lock = selfcheck._lock(self.root, SID)
        self.assertIsNotNone(lock)
        self.assertIn("다른 점검", self.check()["skipped"])
        selfcheck._unlock(lock)
        self.assertEqual(self.check()["human_turns"], 1)
        st = read_json(local_dir(self.root) / "selfcheck-done.json")
        self.assertEqual(st["cursors"][SID]["runs"], 1)
