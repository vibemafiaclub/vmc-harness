import json
from unittest import mock

from tests.util import HarnessTestCase

from vmc_harness.core import events, ledger, notices
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.ledger import selfcheck, stale_docs, why
from vmc_harness.sessions import Session


class WhyTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.write("be/a.py", "def foo():\n    return 1\n")
        self.commit("base")
        write_json(self.root / ".harness" / "rules.json", {"rules": [{"id": "R-1", "basis": {"type": "explicit_request", "quote": "앞으로 SQL은 SQL 폴더에만", "source": {"date": "2026-10-22"}}, "enabled": True}]})
        self.rule_action = ledger.create_action(
            self.root, {"type": "explicit_request", "quote": "앞으로 SQL은 SQL 폴더에만"}, "rule", "h",
            why_not_higher="기계 판정 불가", human_requested=True, target={"rule_id": "R-1"})
        data = read_json(self.root / ".harness" / "rules.json")
        data["rules"][0]["action_id"] = self.rule_action["action_id"]
        write_json(self.root / ".harness" / "rules.json", data)
        ledger.append_task(self.root, {"task_id": "T-261022-aaaaaa", "session_id": "S1",
                                       "declare": {"intent": "foo 고치기", "scope": ["be/a.py"]},
                                       "pm": {"called": True, "opinion": "trim", "wish": "10분 안에"}})
        from vmc_harness.ledger import handlers
        from vmc_harness.hooks.contract import HookContext
        from vmc_harness.ledger import judge
        judge.mark_run(self.root)
        handlers.on_session_start(HookContext("SessionStart", "claude", {}, root=str(self.root), session_id="S1"))
        events.log(self.root, "gate_fire", session_id="S1", gate_id="g-sql")
        self.write("be/a.py", "def foo():\n    return 2\n")
        self.write("be/b.py", "x = 1\n")
        self.commit("foo 고침\n\nHarness-Task: T-261022-aaaaaa\nHarness-OutOfScope: be/b.py")

    def test_collect_task(self):
        res = why.collect(self.root, "T-261022-aaaaaa")
        kinds = {c["kind"] for c in res["candidates"]}
        self.assertTrue({"rule", "gate", "pm", "commit"} <= kinds, kinds)
        rule = [c for c in res["candidates"] if c["kind"] == "rule"][0]
        self.assertEqual(rule["title"], "앞으로 SQL은 SQL 폴더에만")  # 원문 그대로
        text = why.plain_text(res)
        self.assertIn("finding --blame", text)

    def test_collect_file(self):
        res = why.collect(self.root, "be/a.py")
        self.assertEqual(res["tasks"], ["T-261022-aaaaaa"])
        self.assertTrue(res["candidates"])

    def test_blame_rule_reverts(self):
        why.blame(self.root, "R-1")
        f = ledger.list_findings(self.root)[0]
        self.assertEqual(f["kind"], "blame")
        self.assertEqual(ledger.get_action(self.root, self.rule_action["action_id"])["status"], "reverted")
        r = read_json(self.root / ".harness" / "rules.json")["rules"][0]
        self.assertEqual((r["status"], r["enabled"]), ("reverted", False))

    def test_blame_commit(self):
        sha = __import__("subprocess").run(["git", "rev-parse", "HEAD"], cwd=self.root, capture_output=True, text=True).stdout.strip()
        why.blame(self.root, sha[:10])
        self.assertEqual(ledger.list_findings(self.root)[0]["kind"], "blame")
        self.assertFalse((self.root / "be" / "b.py").exists())


def _session(prompts, sid="S1"):
    return Session(session_id=sid, agent="claude", human_prompts=[{"at": "2026-10-22T10:00:00+09:00", "text": p} for p in prompts],
                   tool_calls=[{"name": "Edit"}] * 10, human_turns=len(prompts), measured=True)


def _llm(out):
    return {"text": json.dumps(out, ensure_ascii=False), "tokens": 100, "sec": 1.0}


class SelfcheckTest(HarnessTestCase):
    def _run(self, session, out, sid="S1"):
        with mock.patch("vmc_harness.sessions.parse", return_value=session), \
                mock.patch("vmc_harness.core.llm.available", return_value=True), \
                mock.patch("vmc_harness.core.llm.claude_p", return_value=_llm(out)):
            return selfcheck.run_session(self.root, "/x/%s.jsonl" % sid, "claude", sid)

    def test_learning_needs_two(self):
        l = {"same_as": None, "gist": "한 페이지 먼저 확인", "title": "한 페이지 먼저", "situation": "디자인 변경",
             "judgment": "전체 적용 전에 한 페이지로 확인", "signals": ["페이지 수정"], "not_when": "급할 때",
             "cross_project": True, "why_not_higher": "취향이라 기계 판정 불가", "prompt_index": 0, "quote": "한 페이지만 먼저"}
        r1 = self._run(_session(["아니 한 페이지만 먼저 해줘"]), {"corrections": 1, "learnings": [l], "explicit_requests": []})
        self.assertNotIn("card", r1["learnings"][0])
        cid = r1["learnings"][0]["candidate_id"]
        c = ledger.list_candidates(self.root)[0]
        self.assertIsNotNone(c["quote_hash"])
        self.assertNotIn("한 페이지", json.dumps(c, ensure_ascii=False).replace(c["summary"], ""))
        r2 = self._run(_session(["또 전체에 했네, 한 페이지만 먼저"], "S2"),
                       {"corrections": 1, "learnings": [dict(l, same_as=cid, quote="한 페이지만 먼저")], "explicit_requests": []},
                       sid="S2")
        card = r2["learnings"][0]["card"]
        a = ledger.get_action(self.root, r2["learnings"][0]["action_id"])
        self.assertEqual((a["layer"], a["basis"]["type"], a["status"]), ("card", "learning", "trial"))
        from vmc_harness.brain import store
        self.assertEqual(store.get_card(card)["origin"], "learning")
        self.assertEqual(len(events.read(self.root, "session_selfcheck")), 2)

    def test_explicit_rule_verbatim_and_checks(self):
        out = {"corrections": 0, "learnings": [], "explicit_requests": [
            {"prompt_index": 0, "quote": "앞으로 SQL 파일은 SQL 폴더에만 만들어", "kind": "rule", "title": "SQL 폴더"},
            {"prompt_index": 1, "quote": "지어낸 문장", "kind": "rule"},
            {"prompt_index": 1, "quote": "이건 지금만", "kind": "rule"}]}
        r = self._run(_session(["앞으로 SQL 파일은 SQL 폴더에만 만들어", "이건 지금만 이렇게"]), out)
        self.assertEqual(len(r["explicit"]), 1)
        rules = read_json(self.root / ".harness" / "rules.json")["rules"]
        self.assertEqual(rules[0]["basis"]["quote"], "앞으로 SQL 파일은 SQL 폴더에만 만들어")
        self.assertTrue(rules[0]["basis"]["source"]["date"])
        a = ledger.get_action(self.root, rules[0]["action_id"])
        self.assertTrue(a["human_requested"])
        self.assertEqual(a["basis"]["quote"], "앞으로 SQL 파일은 SQL 폴더에만 만들어")
        # 같은 세션의 이미 본 구간은 다시 안 본다
        self.assertEqual(self._run(_session(["x"]), out)["skipped"], "새 구간 없음")

    def test_block_without_gate_generator_becomes_rule(self):
        out = {"corrections": 0, "learnings": [], "explicit_requests": [
            {"prompt_index": 0, "quote": "앞으로 운영 DB 직접 접속은 막아", "kind": "block"}]}
        with mock.patch("vmc_harness.gates.generate.from_request", return_value=None):
            r = self._run(_session(["앞으로 운영 DB 직접 접속은 막아"]), out)
        self.assertEqual(r["explicit"][0]["kind"], "rule")

    def test_block_delegates_to_gate_generator(self):
        out = {"corrections": 0, "learnings": [], "explicit_requests": [
            {"prompt_index": 0, "quote": "앞으로 정산 페이지 폭은 막아", "kind": "block"}]}
        with mock.patch("vmc_harness.gates.generate.from_request",
                        return_value={"gate_id": "g-x", "action_id": "A-1"}) as gen:
            r = self._run(_session(["앞으로 정산 페이지 폭은 막아"]), out)
        self.assertEqual(r["explicit"][0], {"kind": "gate", "gate_id": "g-x", "action_id": "A-1"})
        self.assertEqual(gen.call_args.kwargs["quote"], "앞으로 정산 페이지 폭은 막아")

    def test_llm_failure_still_counts(self):
        with mock.patch("vmc_harness.sessions.parse", return_value=_session(["a", "b"])), \
                mock.patch("vmc_harness.core.llm.available", return_value=True), \
                mock.patch("vmc_harness.core.llm.claude_p", return_value=None):
            r = selfcheck.run_session(self.root, "/x/S9.jsonl", "claude", "S9")
        self.assertEqual(r["human_turns"], 2)
        self.assertEqual(events.read(self.root, "session_selfcheck")[0]["tool_calls"], 10)


class StaleDocsTest(HarnessTestCase):
    def test_detect_and_notice(self):
        self.write("be/svc.py", "def submit_request():\n    pass\n")
        self.write("docs/guide.md", "`be/svc.py`의 `submit_request()`를 쓴다. 옛 `be/old_svc.py`와 `approveRequest()`.\n")
        self.write("docs/ok.md", "`be/svc.py` 참고\n")
        self.write("AGENTS.md", "고객 문서 `be/svc.py`\n")
        self.commit("docs")
        before = (self.root / "AGENTS.md").read_text()
        data = stale_docs.scan(self.root)
        self.assertEqual(set(data["docs"]), {"docs/guide.md"})
        self.assertEqual(set(data["docs"]["docs/guide.md"]["missing"]), {"be/old_svc.py", "approveRequest()"})
        self.assertTrue(stale_docs.is_stale(self.root, "docs/guide.md"))
        self.assertEqual(stale_docs.notice_for(self.root, ["be/svc.py"]), [])
        self.assertEqual(len(stale_docs.notice_for(self.root, ["docs/new.md"])), 1)
        self.assertEqual(stale_docs.notice_for(self.root, ["docs/guide.md"]), [])  # 한 번만
        self.assertEqual((self.root / "AGENTS.md").read_text(), before)
        self.assertTrue(any(n["kind"] == "stale_doc" for n in notices.pending(self.root)))


class GuideConflictTest(HarnessTestCase):
    """정정 발화가 그 작업에 로드된 하네스 지침과 부딪히면 원복 후보 알림 — 후보일 뿐 finding·장부를 움직이지 않는다."""

    def setUp(self):
        super().setUp()
        self.write("be/a.py", "x = 1\n")
        self.commit("base")
        write_json(self.root / ".harness" / "rules.json", {"rules": [
            {"id": "R-1", "basis": {"type": "explicit_request", "quote": "앞으로 전체 페이지에 한 번에 적용"}, "enabled": True},
            {"id": "R-2", "text": "사람이 쓴 규칙", "enabled": True}]})
        self.action = ledger.create_action(
            self.root, {"type": "explicit_request", "quote": "앞으로 전체 페이지에 한 번에 적용"}, "rule", "h",
            why_not_higher="기계 판정 불가", human_requested=True, target={"rule_id": "R-1"})
        data = read_json(self.root / ".harness" / "rules.json")
        data["rules"][0]["action_id"] = self.action["action_id"]
        write_json(self.root / ".harness" / "rules.json", data)
        ledger.append_task(self.root, {"task_id": "T-261023-bbbbbb", "session_id": "S1",
                                       "declare": {"intent": "목록 페이지 폭", "scope": ["be/a.py"]}})
        events.log(self.root, "rules_loaded", session_id="S1", rule_ids=["R-1", "R-2"])
        events.log(self.root, "gate_fire", session_id="S1", gate_id="g-kernel")  # 생성 게이트 아님 → 후보 아님

    def _run(self, out, sid="S1", prompts=("아니 전체 말고 이 페이지만 바꿔",)):
        with mock.patch("vmc_harness.sessions.parse", return_value=_session(list(prompts), sid)), \
                mock.patch("vmc_harness.core.llm.available", return_value=True), \
                mock.patch("vmc_harness.core.llm.claude_p", return_value=_llm(out)) as m:
            r = selfcheck.run_session(self.root, "/x/%s.jsonl" % sid, "claude", sid)
        return r, m

    def _out(self, **c):
        return {"corrections": 1, "learnings": [], "explicit_requests": [],
                "conflicts": [dict({"prompt_index": 0, "quote": "전체 말고 이 페이지만", "guide_id": "R-1"}, **c)]}

    def test_guides_are_mechanical_and_revertible_only(self):
        g = selfcheck.session_guides(self.root, "S1")
        self.assertEqual(list(g), ["R-1"])  # 사람이 쓴 규칙(R-2)·생성 게이트 아닌 것 제외
        self.assertEqual(g["R-1"]["title"], "앞으로 전체 페이지에 한 번에 적용")
        self.assertEqual(selfcheck.session_guides(self.root, "S-other"), {})

    def test_conflict_becomes_notice_once_and_moves_nothing(self):
        r, m = self._run(self._out())
        self.assertIn("R-1: 앞으로 전체 페이지에 한 번에 적용", m.call_args[0][0])  # 후보 id 목록을 넘긴다
        self.assertEqual(len(r["conflicts"]), 1)
        n = [x for x in notices.pending(self.root) if x["kind"] == "guide_conflict"]
        self.assertEqual(len(n), 1)
        self.assertEqual(n[0]["buttons"], ["원복", "아님"])
        self.assertEqual(n[0]["refs"]["guide_id"], "R-1")
        self.assertEqual(n[0]["text"], "'전체 말고 이 페이지만' — 이 지침과 부딪힌 것 같음: 앞으로 전체 페이지에 한 번에 적용")
        self.assertEqual(ledger.list_findings(self.root), [])
        self.assertEqual(ledger.get_action(self.root, self.action["action_id"])["status"], "trial")
        # 같은 지침은 다시 띄우지 않는다
        ledger.append_task(self.root, {"task_id": "T-261023-cccccc", "session_id": "S2", "declare": {"scope": ["be/a.py"]}})
        events.log(self.root, "rules_loaded", session_id="S2", rule_ids=["R-1"])
        r2, _ = self._run(self._out(), sid="S2")
        self.assertEqual(r2["conflicts"], [])
        self.assertEqual(len([x for x in notices.pending(self.root) if x["kind"] == "guide_conflict"]), 1)

    def test_unknown_id_and_bad_quote_dropped(self):
        r, _ = self._run({"corrections": 1, "learnings": [], "explicit_requests": [], "conflicts": [
            {"prompt_index": 0, "quote": "전체 말고 이 페이지만", "guide_id": "R-2"},      # 목록 밖(사람이 쓴 규칙)
            {"prompt_index": 0, "quote": "전체 말고 이 페이지만", "guide_id": "K-none"},   # 지어낸 id
            {"prompt_index": 0, "quote": "지어낸 말", "guide_id": "R-1"},                  # 원문에 없음
            {"prompt_index": 0, "quote": "전체 말고 이 페이지만", "guide_id": None}]})
        self.assertEqual(r["conflicts"], [])
        self.assertFalse([x for x in notices.pending(self.root) if x["kind"] == "guide_conflict"])

    def test_no_guides_no_list(self):
        r, m = self._run(self._out(), sid="S9")
        self.assertIn("하네스가 넣은 지침:\n(없음)", m.call_args[0][0])
        self.assertEqual(r["conflicts"], [])

    def _cli(self, *argv):
        import io
        import os
        from contextlib import redirect_stdout
        from vmc_harness import cli
        old = os.getcwd()
        os.chdir(str(self.root))
        try:
            with redirect_stdout(io.StringIO()) as buf, mock.patch("vmc_harness.core.proc.spawn_detached"):
                rc = cli.main(list(argv))
        finally:
            os.chdir(old)
        return rc, buf.getvalue()

    def test_revert_button_same_path_as_blame(self):
        self._run(self._out())
        nid = [x for x in notices.pending(self.root) if x["kind"] == "guide_conflict"][0]["id"]
        rc, _ = self._cli("notice", "revert", nid)
        self.assertEqual(rc, 0)
        f = ledger.list_findings(self.root)
        self.assertEqual((len(f), f[0]["kind"]), (1, "blame"))
        self.assertEqual(ledger.get_action(self.root, self.action["action_id"])["status"], "reverted")
        self.assertFalse([x for x in notices.pending(self.root) if x["kind"] == "guide_conflict"])

    def test_dismiss_records_only(self):
        self._run(self._out())
        nid = [x for x in notices.pending(self.root) if x["kind"] == "guide_conflict"][0]["id"]
        rc, _ = self._cli("notice", "dismiss", nid)
        self.assertEqual(rc, 0)
        self.assertEqual(ledger.list_findings(self.root), [])
        self.assertEqual(ledger.get_action(self.root, self.action["action_id"])["status"], "trial")
        self.assertEqual(len(events.read(self.root, "guide_conflict_dismissed")), 1)
        self.assertFalse([x for x in notices.pending(self.root) if x["kind"] == "guide_conflict"])

    def test_text_brief_shows_command(self):
        self._run(self._out())
        rc, out = self._cli("brief")
        self.assertEqual(rc, 0)
        self.assertIn("원복: vmc-harness finding --blame R-1", out)

    def test_cli_blame_closes_notice(self):
        self._run(self._out())
        rc, _ = self._cli("finding", "--blame", "R-1")  # mod 없이 명령으로 원복해도 같은 경로
        self.assertEqual(rc, 0)
        self.assertEqual(ledger.get_action(self.root, self.action["action_id"])["status"], "reverted")
        self.assertFalse([x for x in notices.pending(self.root) if x["kind"] == "guide_conflict"])
