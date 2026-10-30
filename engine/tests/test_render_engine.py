"""렌더링 엔진: 기계 검사 2개, 실패할 때만 1회 재작성, 규칙 원문은 그대로 (spec 08)."""

import unittest
from unittest import mock

from tests.util import HarnessTestCase

from vmc_harness.render import checks, engine, lexicon, profile

READER = {"name": "r", "preferred": {"원복": {"term": "원복", "variant": "원복", "counts": {"원복": 5, "롤백": 0, "되돌리": 0}}},
          "vocab": {"sp": 64, "반려": 10, "재상신": 4, "회수": 3, "pass": 10}, "objects": ["usp_WorkRequest_Reject"],
          "samples": ["반려하고 재상신하면 오류나는거 원인 찾아서 고쳐줘"], "avoid": []}


class ChecksTest(unittest.TestCase):
    def test_facts_numbers_ids_dates_names(self):
        facts = {"affected_rows": 2, "task_id": "T-261028-ab12cd", "at": "2026-10-28T10:00:00+09:00",
                 "sp": "usp_WorkRequest_Reject", "count": 1042, "summary": "긴 서술 문장은 풀어 써도 된다"}
        ok = "usp_WorkRequest_Reject 수정 · 영향 행 2 · 10/28 · T-261028-ab12cd · 1,042건"
        self.assertEqual(checks.missing_facts(ok, facts), [])
        self.assertEqual(set(checks.missing_facts("수정했어요 영향 행 22", facts)),
                         {"2", "T-261028-ab12cd", "2026-10-28T10:00:00+09:00", "usp_WorkRequest_Reject", "1042"})

    def test_unknown_words_korean_tokenizing(self):
        v = checks.Vocab(words=["반려", "재상신", "회수", "sp"])
        self.assertEqual(checks.unknown_words("원인 찾았어요. 반려할 때 회수 상태가 안 풀려서 재상신이 막혀요.", v), [])
        self.assertEqual(checks.unknown_words("반려 sp를 고쳐줘서 2건에 바로 적용됨", v), [])
        unk = checks.unknown_words("멱등성 보장을 위해 트랜잭션을 썼습니다", v)
        self.assertIn("멱등성", unk)
        self.assertIn("트랜잭션", unk)

    def test_discouraged_synonym_and_avoid(self):
        v = checks.Vocab(words=[], avoid=["멱등"], discouraged={"롤백": "원복"})
        unk = checks.unknown_words("롤백 가능 · 멱등하게 처리", v)
        self.assertIn("롤백", unk)
        self.assertIn("멱등", unk)

    def test_code_names_not_words(self):
        v = checks.Vocab()
        self.assertEqual(checks.unknown_words("menu.tsx · work_requests · getUserList 수정", v), [])


class EngineTest(HarnessTestCase):
    def _llm(self, text):
        return mock.patch("vmc_harness.core.llm.claude_p", return_value={"text": text, "tokens": 1, "sec": 0.1})

    def test_pass_no_llm_call(self):
        with self._llm("x") as m:
            r = engine.render(self.root, READER, {"affected_rows": 2}, draft="반려 sp 수정 · 영향 행 2")
        self.assertTrue(r["checks"]["facts_ok"])
        self.assertEqual(r["checks"]["unknown_words"], [])
        self.assertEqual(r["llm_calls"], 0)
        m.assert_not_called()

    def test_fail_then_one_rewrite(self):
        with self._llm("반려 sp 수정 · 영향 행 2 · 원복 가능") as m:
            r = engine.render(self.root, READER, {"affected_rows": 2}, draft="반려 sp 수정 · 영향 행 2 · 롤백 가능")
        self.assertEqual(m.call_count, 1)
        self.assertTrue(r["rewritten"])
        self.assertEqual(r["checks"]["unknown_words"], [])
        # 재작성 지시: 말투 흉내 금지, 남길 값, 쓰지 말 말
        system = m.call_args.kwargs["system"]
        self.assertIn("말투", system)
        self.assertIn("롤백", m.call_args.args[0])

    def test_rewrite_losing_facts_rejected(self):
        with self._llm("반려 sp 수정 · 원복 가능"):
            r = engine.render(self.root, READER, {"affected_rows": 2}, draft="반려 sp 수정 · 영향 행 2 · 롤백 가능")
        self.assertFalse(r["rewritten"])
        self.assertIn("2", r["text"])
        self.assertEqual(r["llm_calls"], 1)

    def test_rule_passthrough(self):
        with self._llm("바뀐 글") as m:
            r = engine.render(self.root, READER, {}, draft="앞으로 SQL 폴더 밖 sql 쓰기는 막아", kind="rule")
        self.assertEqual(r["text"], "앞으로 SQL 폴더 밖 sql 쓰기는 막아")
        m.assert_not_called()

    def test_llm_unavailable_fail_open(self):
        with mock.patch("vmc_harness.core.llm.claude_p", return_value=None):
            r = engine.render(self.root, READER, {}, draft="멱등성 확인")
        self.assertFalse(r["rewritten"])
        self.assertIn("멱등성", r["checks"]["unknown_words"])

    def test_lexicon_terms_known(self):
        lexicon.merge_terms(self.root, ["상신함"])
        r = engine.render(self.root, {}, {}, draft="상신함 확인", allow_llm=False)
        self.assertEqual(r["checks"]["unknown_words"], [])

    def test_evaluate_regression_set(self):
        cases = [{"id": "1", "facts": {"n": 3}, "draft": "결재 시나리오 3건 pass"},
                 {"id": "2", "facts": {"n": 4}, "draft": "멱등성 문제 3건"}]
        out = engine.evaluate(self.root, READER, cases, allow_llm=False)
        self.assertEqual(out["summary"]["n"], 2)
        self.assertEqual(out["summary"]["facts_ok_rate"], 0.5)
        self.assertEqual(out["cases"][0]["unknown_count"], 0)
        self.assertGreaterEqual(out["summary"]["unknown_total"], 1)


if __name__ == "__main__":
    unittest.main()
