"""승인 카드: 4줄·[자세히]·운영 SQL 필수 항목·거절=finding·응답 시간 참고 지표 (spec 08)."""

from unittest import mock

from tests.util import HarnessTestCase

from vmc_harness.core import ledger
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core import paths
from vmc_harness.render import approval_card


def _db(root, detail):
    return ledger.create_approval(root, "T-261028-ab12cd", 1, "반려 sp 수정", "approve", True, "롤백 sql 실행",
                                  ["결재 시나리오 3건 pass"], 6, detail=detail)


class ApprovalCardTest(HarnessTestCase):
    def test_db_card_requires_dryrun_and_rollback(self):
        p = _db(self.root, {"kind": "db_apply", "reason": "회수 상태가 안 풀리던 원인 수정"})
        with self.assertRaises(approval_card.CardError):
            approval_card.card(self.root, p["approval_id"], allow_llm=False)
        p2 = _db(self.root, {"kind": "db_apply", "dryrun": {"affected_rows": 2}})
        with self.assertRaises(approval_card.CardError):
            approval_card.card(self.root, p2["approval_id"], allow_llm=False)

    def test_db_card_four_lines(self):
        p = _db(self.root, {"kind": "db_apply", "dryrun": {"affected_rows": 2}, "rollback_sql_path": "SQL/rollback_1.sql",
                            "reason": "회수 상태가 안 풀리던 원인 수정", "files": ["SQL/SP/usp_WorkRequest_Reject.sql"],
                            "diff": "+ a\n- b"})
        with mock.patch("vmc_harness.core.llm.claude_p", return_value=None):
            c = approval_card.card(self.root, p["approval_id"])
        self.assertLessEqual(len(c["lines"]), 4)
        self.assertEqual(c["buttons"], ["승인", "거절", "자세히"])
        self.assertIn("반려 sp 수정", c["lines"][0])
        self.assertTrue(any("영향 행 2" in l for l in c["lines"]))
        self.assertTrue(any("원복 가능" in l for l in c["lines"]))
        self.assertNotIn("usp_WorkRequest_Reject.sql", c["text"])  # 영향 파일은 [자세히]에서만
        d = approval_card.detail(self.root, p["approval_id"])
        self.assertEqual(d["files"], ["SQL/SP/usp_WorkRequest_Reject.sql"])
        self.assertEqual(d["dryrun"]["affected_rows"], 2)
        self.assertIn("rollback_1.sql", approval_card.detail_text(self.root, p["approval_id"]))

    def test_diff_limit(self):
        p = ledger.create_approval(self.root, "T-1", 2, "보호 영역 변경", "approve", True, "git revert", ["ok"], 6,
                                   detail={"diff": "\n".join("+x" for _ in range(80))})
        with self.assertRaises(approval_card.CardError):
            approval_card.card(self.root, p["approval_id"], allow_llm=False)

    def test_answer_reject_is_finding_and_timing(self):
        p = ledger.create_approval(self.root, "T-1", 2, "보호 영역 변경", "approve", True, "git revert", ["ok"], 3)
        approval_card.mark_shown(self.root, p["approval_id"])
        rec = approval_card.answer(self.root, p["approval_id"], "reject")
        self.assertEqual(rec["answer"], "reject")
        self.assertEqual(ledger.list_findings(self.root)[0]["kind"], "reject")
        self.assertIsNotNone(approval_card.answer_sec(rec))
        # 3분 넘는 유형 참고 지표
        f = paths.ledger_dir(self.root) / "approvals" / ("%s.json" % p["approval_id"])
        r = read_json(f)
        r["shown_at"], r["answered_at"] = "2026-10-28T10:00:00+09:00", "2026-10-28T10:05:00+09:00"
        write_json(f, r)
        slow = approval_card.slow_types(self.root)
        self.assertTrue(slow[0]["slow"])
        self.assertEqual(slow[0]["median_sec"], 300.0)
