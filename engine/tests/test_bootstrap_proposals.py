"""bootstrap: 진단 제안 → 장치 변환이 뜻을 지키는가 (B1~B3 재현 — 합성 제안 JSON, LLM 없음).

- B1 "push 직전에 검사를 자동으로 돌리기" → 막는 게이트가 아니라 구조 티켓
- B2 "위 명령이 생긴 뒤 켭니다" → requires 없으면 형식 오류, requires가 있으면 꺼진 채 생성 → 조건 충족 후 켜짐
- B3 rule_text 없음·할 일 문장 → 형식 오류. 옛 진단 규칙(rule_text 없음)은 AGENTS.md 블록에서 빠지고 알림 한 줄
"""

import json
from unittest import mock

from tests.util import HarnessTestCase
from vmc_harness.bootstrap import diagnose, facts, requires
from vmc_harness.core.jsonio import read_json, read_jsonl, write_json


def _p(**kw):
    base = {"evidence": "지난 기록 3건", "blocks": "b", "scope": "s", "cost": "작업당 0.1초"}
    base.update(kw)
    return base


EXEC_GATE = _p(title="push 직전에 CI와 같은 로컬 검사를 자동으로 돌리기(pytest, typecheck)", kind="gate",
               condition={"type": "command", "regex": r"git\s+push"})
BLOCK_GATE = _p(title="운영 DB에 SQL 직접 실행 막기", kind="gate", condition={"type": "command", "regex": r"sqlcmd"})
COND_GATE = _p(title="브라우저 테스트 없이 화면 파일 커밋 막기 — npm run test:browser가 생긴 뒤 켭니다", kind="block",
               condition={"type": "path_write", "globs": ["web/**"]})
GOOD_RULE = _p(title="화면 안내 문구 규칙", kind="rule", rule_text="요청하지 않은 화면 안내 문구는 넣지 않는다.")
TODO_RULE1 = _p(title="운영 제약 절", kind="rule",
                rule_text="작업을 넘길 때마다 다시 적는 운영 제약을 AGENTS.md의 '운영 제약' 절로 옮기기")
TODO_RULE2 = _p(title="안내 문구", kind="rule",
                rule_text="'요청하지 않은 화면 안내 문구는 넣지 않는다'를 메모에서 AGENTS.md 규칙으로 올리기")
NO_TEXT_RULE = _p(title="'요청하지 않은 안내 문구는 넣지 않는다'를 규칙으로 올리기", kind="rule")


class ProposalKindTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.write("web/a.ts", "export const a = 1\n")
        self.write("package.json", json.dumps({"name": "x", "scripts": {"test": "echo ok"}}))
        self.commit("base")
        facts.collect(self.root, run_tests=False, use_llm=False)

    def _run_with(self, data):
        fake = {"text": json.dumps(data, ensure_ascii=False), "tokens": 10, "sec": 0.1}
        with mock.patch("vmc_harness.core.llm.available", return_value=True), \
                mock.patch("vmc_harness.core.llm.nested", return_value=False), \
                mock.patch("vmc_harness.core.llm.claude_agent_capped", return_value=fake), \
                mock.patch.object(diagnose, "render_proposal", return_value={}):
            return diagnose.run(self.root, sessions=[])

    # ---- 형식 검사 ----

    def test_exec_gate_becomes_structure(self):
        ok, err = diagnose.validate({"proposals": [dict(EXEC_GATE), dict(BLOCK_GATE)]})
        self.assertEqual(err, [])
        self.assertEqual(ok[0]["kind"], "structure")
        self.assertEqual(ok[0]["converted_from"], "gate")
        self.assertNotIn("condition", ok[0])
        self.assertEqual(ok[1]["kind"], "gate")  # 막는 말이 있으면 실행이 들어가도 게이트

    def test_rule_text_required_and_not_todo(self):
        ok, err = diagnose.validate({"proposals": [dict(GOOD_RULE), dict(TODO_RULE1), dict(TODO_RULE2), dict(NO_TEXT_RULE)]})
        self.assertEqual([p["title"] for p in ok], ["화면 안내 문구 규칙"])
        self.assertEqual(len(err), 3)
        self.assertTrue(all("rule_text" in e for e in err), err)
        self.assertIsNone(diagnose.rule_text_error("커밋 메시지는 한국어로 쓴다"))
        self.assertIsNotNone(diagnose.rule_text_error("Move the constraint section to AGENTS.md"))

    def test_conditional_enable_needs_requires(self):
        ok, err = diagnose.validate({"proposals": [dict(COND_GATE)]})
        self.assertEqual(ok, [])
        self.assertIn("requires", err[0])
        withreq = dict(COND_GATE, requires=[{"type": "npm_script", "name": "test:browser"}])
        ok, err = diagnose.validate({"proposals": [withreq]})
        self.assertEqual(err, [])
        self.assertEqual(ok[0]["kind"], "gate")  # block은 gate의 다른 이름
        bad = dict(COND_GATE, requires=[{"type": "file_exists", "path": "/etc/passwd"}])
        self.assertTrue(diagnose.validate({"proposals": [bad]})[1])

    def test_prompt_mentions_kinds_and_fields(self):
        inputs = diagnose.prepare_inputs(self.root, [], None)
        p = diagnose.build_prompt(self.root, inputs, [])
        for s in ('"rule_text"', '"requires"', "structure다", "requires에 넣는다"):
            self.assertIn(s, p)

    # ---- [추가] → 장치 ----

    def test_accept_each_kind(self):
        gate = dict(COND_GATE, title="화면 파일 커밋 막기 — npm run test:browser가 생긴 뒤 켭니다",
                    requires=[{"type": "npm_script", "name": "test:browser"}])
        rule_req = dict(GOOD_RULE, title="lint 규칙", rule_text="커밋 전에 npm run lint:strict 결과를 확인한다.",
                        requires=[{"type": "file_exists", "path": ".gitattributes"}])
        d = self._run_with({"summary": "s", "proposals": [dict(EXEC_GATE), gate, dict(GOOD_RULE), rule_req]})
        kinds = {p["no"]: p["kind"] for p in d["proposals"]}
        self.assertEqual(kinds, {1: "structure", 2: "gate", 3: "rule", 4: "rule"})
        self.assertTrue(d["proposals"][0].get("ticket_id"))  # 실행형은 루프 티켓으로
        res = {r["no"]: r for r in diagnose.accept(self.root, [2, 3, 4])}
        self.assertFalse(any(r.get("error") for r in res.values()), res)
        meta = read_json(self.root / ".harness" / "gates" / ("%s.json" % res[2]["gate_id"]))
        self.assertFalse(meta["enabled"])  # 전제가 안 맞으면 꺼진 채
        self.assertEqual(meta["disabled_reason"], "requires")
        self.assertEqual(res[2]["waiting"], ["npm run test:browser 있음"])
        agents = (self.root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("요청하지 않은 화면 안내 문구는 넣지 않는다.", agents)  # 규칙 문장 그대로
        self.assertNotIn("화면 안내 문구 규칙", agents)  # 제목(할 일)이 아니라
        self.assertNotIn("lint:strict", agents)  # 전제가 안 맞은 규칙은 아직 안 싣는다
        # 전제 충족 → 밤 루프가 켠다
        self.assertEqual(requires.enable_ready(self.root), {"enabled": [], "waiting": 2})
        self.write("package.json", json.dumps({"name": "x", "scripts": {"test": "x", "test:browser": "y"}}))
        self.write(".gitattributes", "* text=auto\n")
        r = requires.enable_ready(self.root)
        self.assertEqual(len(r["enabled"]), 2)
        self.assertTrue(read_json(self.root / ".harness" / "gates" / ("%s.json" % res[2]["gate_id"]))["enabled"])
        self.assertIn("lint:strict", (self.root / "AGENTS.md").read_text(encoding="utf-8"))

    def test_old_diagnosis_rule_without_text_not_added(self):
        """옛 진단 결과(rule_text 칸 없음)의 rule을 [추가]하면 할 일 제목을 규칙으로 넣지 않는다."""
        write_json(diagnose.diagnosis_path(self.root), {"proposals": [dict(NO_TEXT_RULE, no=1)]})
        r = diagnose.accept(self.root, [1])[0]
        self.assertIn("rule_text", r["error"])
        self.assertFalse((self.root / ".harness" / "rules.json").exists()
                         and read_json(self.root / ".harness" / "rules.json").get("rules"))

    def test_migration_hides_title_rules(self):
        """기존 설치분: rule_text 없는 진단 규칙 → 블록에서 빼고 알림 한 줄(한 번만)."""
        from vmc_harness.core.project import local_dir
        from vmc_harness.gates import rules as grules
        src = {"session": None, "date": "2026-10-23", "area": "s", "via": "diagnose accept"}
        write_json(grules.rules_path(self.root), {"rules": [
            {"id": "r-old1", "basis": {"type": "explicit_request", "quote": TODO_RULE2["rule_text"], "source": src},
             "enabled": True},
            {"id": "r-human", "basis": {"type": "explicit_request", "quote": "테스트는 지우지 마",
                                        "source": {"session": "abc", "date": "2026-10-24", "area": "be/"}}, "enabled": True}]})
        # 0.1.4가 그려 둔 블록(할 일 제목이 규칙으로 들어감) — 업데이트 뒤 첫 세션 시작이 다시 그린다
        (self.root / "AGENTS.md").write_bytes(("# 팀 지침\r\n\r\n%s\r\n- \"%s\"\r\n%s\r\n" % (
            grules.BEGIN, TODO_RULE2["rule_text"], grules.END)).encode("utf-8"))
        grules.refresh_machine_facts(self.root)
        raw = (self.root / "AGENTS.md").read_bytes()
        self.assertTrue(raw.startswith("# 팀 지침\r\n\r\n".encode("utf-8")))
        agents = raw.decode("utf-8")
        self.assertNotIn("규칙으로 올리기", agents)
        self.assertIn("테스트는 지우지 마", agents)  # 사람 원문 규칙은 그대로
        grules.render(self.root)
        notes = [n for n in read_jsonl(local_dir(self.root) / "notices.jsonl") if n["kind"] == "rule_needs_text"]
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["refs"]["rule_ids"], ["r-old1"])
