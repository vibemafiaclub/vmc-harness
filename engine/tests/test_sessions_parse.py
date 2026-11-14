"""세션 파서 — 합성 픽스처(고객·실제 세션 데이터 없음)."""

import json
import os
from pathlib import Path

from tests.util import HarnessTestCase

from vmc_harness import sessions


def _w(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write((json.dumps(r) if not isinstance(r, str) else r) + "\n")
    return path


def _usage(i=10, o=5, cc=0, cr=100):
    return {"input_tokens": i, "output_tokens": o, "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr}


def claude_rows(cwd, sid="s1"):
    base = {"sessionId": sid, "cwd": cwd}
    return [
        {"type": "mode", "mode": "x", "sessionId": sid},  # 시각 없는 메타
        dict(base, type="user", timestamp="2026-10-22T10:00:00Z", message={"role": "user", "content": "버튼 고쳐줘"}),
        dict(base, type="assistant", timestamp="2026-10-22T10:00:10Z",
             message={"id": "m1", "usage": _usage(), "content": [{"type": "text", "text": "볼게요"}]}),
        # 같은 응답의 다음 블록 — usage 중복(한 번만 세야 한다)
        dict(base, type="assistant", timestamp="2026-10-22T10:00:11Z",
             message={"id": "m1", "usage": _usage(), "content": [
                 {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": cwd + "/a.py"}}]}),
        dict(base, type="user", timestamp="2026-10-22T10:00:20Z",
             message={"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}),
        dict(base, type="assistant", timestamp="2026-10-22T10:01:00Z",
             message={"id": "m2", "usage": _usage(1, 1, 0, 0), "content": [{"type": "text", "text": "끝"}]}),
        dict(base, type="system", subtype="stop_hook_summary", timestamp="2026-10-22T10:01:01Z"),
        # 사람이 5분 뒤 다음 메시지(대기 시간은 빼야 한다)
        dict(base, type="user", timestamp="2026-10-22T10:06:01Z", isMeta=True, message={"content": "<meta>"}),
        dict(base, type="user", timestamp="2026-10-22T10:06:01Z", promptSource="system",
             origin={"kind": "task-notification"}, message={"content": "<task-notification>x"}),
        dict(base, type="user", timestamp="2026-10-22T10:06:02Z", message={"content": "<local-command-stdout>x"}),
        dict(base, type="user", timestamp="2026-10-22T10:10:00Z",
             message={"content": [{"type": "text", "text": "하나 더"}]}),
        dict(base, type="assistant", timestamp="2026-10-22T10:10:30Z",
             message={"id": "m3", "usage": _usage(2, 2, 0, 0), "content": [{"type": "text", "text": "네"}]}),
        "{broken json",
    ]


class ClaudeParseTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.croot = self._tmp / "claude"
        os.environ["VMC_CLAUDE_ROOT"] = str(self.croot)
        os.environ["VMC_CODEX_ROOT"] = str(self._tmp / "codex")
        cwd = str(self.root.resolve())
        self.path = _w(self.croot / sessions.claude_dir_name(self.root.resolve()) / "s1.jsonl", claude_rows(cwd))
        _w(self.croot / sessions.claude_dir_name(self.root.resolve()) / "s1" / "subagents" / "agent-x.jsonl", [
            {"type": "assistant", "isSidechain": True, "timestamp": "2026-10-22T10:00:30Z",
             "message": {"id": "sub1", "usage": _usage(100, 0, 0, 0)}}])

    def test_parse(self):
        s = sessions.parse(self.path, "claude")
        self.assertTrue(s.measured)
        self.assertEqual(s.session_id, "s1")
        self.assertEqual(s.human_turns, 2)
        self.assertEqual([p["text"] for p in s.human_prompts], ["버튼 고쳐줘", "하나 더"])
        self.assertEqual([p["text"] for p in s.assistant_texts], ["볼게요", "끝", "네"])   # 도구 호출·서브에이전트 제외
        # 턴1: 10:00:00→10:01:01 = 61초, 턴2: 10:10:00→10:10:30 = 30초 (사람 대기 제외)
        self.assertAlmostEqual(s.agent_sec, 91.0)
        # m1 한 번(115) + m2(2) + m3(4) + 서브에이전트(100)
        self.assertEqual(s.tokens, 115 + 2 + 4 + 100)
        self.assertEqual(len(s.tool_calls), 1)
        self.assertTrue(s.tool_calls[0]["paths"][0].endswith("a.py"))

    def test_find(self):
        found = sessions.find(self.root)
        self.assertEqual([(a, p.name) for a, p in found], [("claude", "s1.jsonl")])
        other = self._tmp / "proj-other"
        other.mkdir()
        self.assertEqual(sessions.find(other), [])
        self.assertEqual(sessions.find_by_id(self.root, "s1")[0], "claude")

    def test_unknown_format_is_unmeasured(self):
        p = _w(self._tmp / "x.jsonl", [
            {"type": "user", "timestamp": "2026-10-22T10:00:00Z", "message": {"content": "hi"}},
            {"type": "assistant", "timestamp": "2026-10-22T10:00:05Z", "message": {"id": "a", "content": []}}])
        self.assertFalse(sessions.parse(p, "claude").measured)
        self.assertFalse(sessions.parse(self._tmp / "missing.jsonl", "claude").measured)


def codex_rows(cwd):
    return [
        {"timestamp": "2026-10-22T10:00:00Z", "type": "session_meta", "payload": {"id": "c1", "cwd": cwd}},
        {"timestamp": "2026-10-22T10:00:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "u1"}},
        {"timestamp": "2026-10-22T10:00:00Z", "type": "response_item",
         "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<environment_context>"}]}},
        {"timestamp": "2026-10-22T10:00:01Z", "type": "event_msg", "payload": {"type": "user_message", "message": "고쳐줘"}},
        {"timestamp": "2026-10-22T10:00:05Z", "type": "response_item",
         "payload": {"type": "custom_tool_call", "name": "apply_patch", "call_id": "x",
                     "input": "*** Begin Patch\n*** Update File: src/a.py\n@@\n-a\n+b\n*** End Patch"}},
        {"timestamp": "2026-10-22T10:00:06Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 50, "cached_input_tokens": 20,
                                                                            "output_tokens": 7, "total_tokens": 57}}}},
        {"timestamp": "2026-10-22T10:00:40Z", "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "u1"}},
        {"timestamp": "2026-10-22T10:30:00Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "u2"}},
        {"timestamp": "2026-10-22T10:30:00Z", "type": "event_msg", "payload": {"type": "user_message", "message": "또"}},
        {"timestamp": "2026-10-22T10:30:10Z", "type": "event_msg",
         "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 80, "output_tokens": 10,
                                                                            "total_tokens": 90}}}},
        {"timestamp": "2026-10-22T10:30:20Z", "type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": "u2"}},
    ]


class CodexParseTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        os.environ["VMC_CLAUDE_ROOT"] = str(self._tmp / "claude")
        os.environ["VMC_CODEX_ROOT"] = str(self._tmp / "codex")
        self.path = _w(self._tmp / "codex" / "2026" / "10" / "22" / "rollout-2026-10-22T10-00-00-c1.jsonl",
                       codex_rows(str(self.root.resolve())))

    def test_parse(self):
        s = sessions.parse(self.path)  # 경로로 codex 판별
        self.assertEqual(s.agent, "codex")
        self.assertTrue(s.measured)
        self.assertEqual(s.session_id, "c1")
        self.assertEqual(s.human_turns, 2)
        self.assertAlmostEqual(s.agent_sec, 40.0 + 20.0)
        self.assertEqual(s.tokens, 90)  # 누적값의 마지막
        self.assertEqual(s.tool_calls[0]["paths"], ["src/a.py"])

    def test_older_format_user_items(self):
        rows = [r for r in codex_rows(str(self.root)) if (r.get("payload") or {}).get("type") != "user_message"]
        rows.insert(3, {"timestamp": "2026-10-22T10:00:01Z", "type": "response_item",
                        "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "고쳐줘"}]}})
        p = _w(self._tmp / "old.jsonl", rows)
        s = sessions.parse(p, "codex")
        self.assertEqual(s.human_turns, 1)
        self.assertTrue(s.measured)

    def test_no_token_info_is_unmeasured(self):
        rows = [r for r in codex_rows(str(self.root)) if (r.get("payload") or {}).get("type") != "token_count"]
        p = _w(self._tmp / "nt.jsonl", rows)
        self.assertFalse(sessions.parse(p, "codex").measured)

    def test_find(self):
        self.assertEqual([a for a, _ in sessions.find(self.root)], ["codex"])
        self.assertEqual(sessions.find(self.root, agent="claude"), [])
