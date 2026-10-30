"""어휘집 양방향 — 사람 → 에이전트(term_context) (spec 08 §렌더링 엔진). 합성 데이터만."""

import json

from tests.util import HarnessTestCase

from vmc_harness.core import inject
from vmc_harness.core.jsonio import write_json
from vmc_harness.hooks import dispatcher
from vmc_harness.hooks.contract import HookContext
from vmc_harness.render import handlers, lexicon


class LexiconBidirTest(HarnessTestCase):
    def _db(self, dev="SAMPLE_DEV", prod=("SAMPLE_PROD",)):
        from vmc_harness.db import client
        client.save_profile(self.root, "query", {"server": "devsrv", "database": dev, "user": "reader"})
        cfg = json.loads((self.root / ".harness" / "config.json").read_text(encoding="utf-8"))
        cfg["db"] = {"prod_names": list(prod)}
        write_json(self.root / ".harness" / "config.json", cfg)

    def _prompt(self, text, sid="s1"):
        ctx = HookContext(event="UserPromptSubmit", agent="claude", raw={}, root=str(self.root),
                          session_id=sid, prompt=text)
        r = handlers.on_user_prompt(ctx)
        return r.inject[0] if r and r.inject else None

    # ---- 모호어 출처 ① 기계 사실
    def test_db_seed_from_machine_facts(self):
        self._db()
        msg = self._prompt("DB에서 결재 상태 확인해줘")
        self.assertTrue(msg.startswith("[vmc-harness:term_context] "))
        self.assertIn("'DB'는 기본적으로 개발 DB(SAMPLE_DEV)를 뜻함", msg)
        self.assertIn("다른 대상은 따로 부름: '운영 DB(SAMPLE_PROD)'", msg)
        ok, kind = dispatcher.valid_message(msg, allow_declare=False)  # 작업 중에도 허용 목록 안
        self.assertTrue(ok)
        self.assertEqual(kind, "term_context")

    def test_no_seed_without_registered_dev_db(self):
        self.assertIsNone(self._prompt("DB 확인해줘"))

    def test_ascii_word_boundary_and_korean_alias(self):
        self._db()
        self.assertIsNone(self._prompt("DBA한테 물어봐", sid="a"))
        self.assertIsNotNone(self._prompt("디비 값 봐줘", sid="b"))

    def test_once_per_session(self):
        self._db()
        self.assertIsNotNone(self._prompt("DB 확인"))
        self.assertIsNone(self._prompt("DB 다시 확인"))
        self.assertIsNotNone(self._prompt("DB 확인", sid="s2"))

    # ---- 모호어 출처 ② 사람 정정(2회 이상만)
    def test_detect_correction(self):
        self.assertEqual(lexicon.detect_correction("아니 DB는 운영 DB를 말한 거야"), ("DB", "운영 DB"))
        self.assertEqual(lexicon.detect_correction("여기서 '상신함'은 결재 대기 목록 말하는 거야"),
                         ("상신함", "결재 대기 목록"))
        self.assertIsNone(lexicon.detect_correction("그거는 운영 DB 말한 거야"))
        self.assertIsNone(lexicon.detect_correction("나는 운영 DB를 말한 거야"))
        self.assertIsNone(lexicon.detect_correction("DB 확인해줘"))

    def test_correction_promotes_after_two_and_overrides_seed(self):
        self._db()
        # 정정한 프롬프트에는 그 단어의 이전 정의를 덧붙이지 않는다
        self.assertIsNone(self._prompt("DB는 운영 DB를 말한 거야", sid="c1"))
        self.assertEqual(lexicon.load(self.root)["ambiguous"], {})  # 1회 — 후보만
        self.assertIn("'DB'는 기본적으로 개발 DB", self._prompt("DB 봐줘", sid="c2"))
        self.assertIsNone(self._prompt("DB는 운영 DB 말하는 거라니까", sid="c3"))
        amb = lexicon.load(self.root)["ambiguous"]["DB"]
        self.assertEqual((amb["default"], amb["source"]), ("운영 DB", "correction"))
        msg = self._prompt("DB 봐줘", sid="c4")
        self.assertIn("'DB'는 기본적으로 운영 DB를 뜻함", msg)
        self.assertNotIn("SAMPLE_DEV", msg)

    def test_promoted_keeps_previous_default_as_alternative(self):
        for _ in range(2):
            lexicon.correct(self.root, "목록", "결재 목록")
        for _ in range(2):
            lexicon.correct(self.root, "목록", "작업 목록")
        amb = lexicon.load(self.root)["ambiguous"]["목록"]
        self.assertEqual(amb["default"], "작업 목록")
        self.assertEqual(amb["alternatives"], ["결재 목록"])

    # ---- 그룹·뜻 적힌 용어·상한
    def test_project_groups_and_defined_terms_only(self):
        lx = lexicon.load(self.root)
        lx["groups"] = [["결재함", "결재 목록"]]
        lx["terms"] = {"상신함": {"kind": "term", "means": "내가 올린 결재 대기 목록"},
                       "usp_WorkRequest_Reject": {"kind": "object"}}
        lexicon.save(self.root, lx)
        msg = self._prompt("결재 목록이랑 상신함 화면 usp_WorkRequest_Reject 확인")
        self.assertIn("'결재함'·'결재 목록'은 같은 것을 가리킴", msg)
        self.assertIn("'상신함'은 내가 올린 결재 대기 목록을 뜻함", msg)
        self.assertNotIn("화면", msg)            # 엔진 내장 그룹(페이지·화면)은 넣지 않는다
        self.assertNotIn("usp_WorkRequest_Reject", msg)  # 뜻 없는 업무 대상 이름은 넣지 않는다

    def test_inject_max(self):
        lx = lexicon.load(self.root)
        lx["terms"] = {"t%d" % i: {"means": "뜻%d" % i} for i in range(6)}
        lexicon.save(self.root, lx)
        msg = self._prompt(" ".join("t%d" % i for i in range(6)))
        self.assertEqual(len(msg.split("\n")), 3)  # render.term_inject_max
        msg2 = self._prompt(" ".join("t%d" % i for i in range(6)))
        self.assertEqual(len(msg2.split("\n")), 3)  # 남은 것만

    def test_numeric_pressure_still_filtered(self):
        self.assertIsNone(inject.message("term_context", "이 프로젝트에서 'x'는 예산 90%를 뜻함"))

    def test_dispatcher_end_to_end(self):
        self._db()
        stdin = json.dumps({"session_id": "e2e", "cwd": str(self.root), "prompt": "DB 테이블 봐줘",
                            "hook_event_name": "UserPromptSubmit"})
        out, code = dispatcher.dispatch("UserPromptSubmit", "claude", stdin, cwd=str(self.root))
        self.assertEqual(code, 0)
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("[vmc-harness:term_context] 이 프로젝트에서 'DB'는", ctx)
