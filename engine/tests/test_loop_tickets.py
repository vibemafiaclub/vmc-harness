"""루프 티켓: 기준선 실패 테스트 복구 · 진단 제안 → 루프 티켓 (spec 06 §티켓). 합성 프로젝트, claude·pytest는 가짜.

pytest가 없는 PC에서도 돌게 run_pytest를 unittest 실제 실행으로 바꿔 끼운다(id 형식은 pytest와 같게 file::Class::method).
"""

import re
import subprocess
import sys
from pathlib import Path
from unittest import mock

from tests.test_loop_util import CALC, LoopTestCase
from vmc_harness.core import ledger, notices
from vmc_harness.core.jsonio import write_json

BROKEN_TEST = '''import unittest

from calc import classify


class CalcTest(unittest.TestCase):
    def test_neg(self):
        self.assertEqual(classify(-1), "neg")

    def test_zero(self):
        self.assertEqual(classify(0), "Zero")
'''

FIXED_TEST = BROKEN_TEST.replace('"Zero"', '"zero"')
WEAK_TEST = BROKEN_TEST.replace('        self.assertEqual(classify(0), "Zero")\n', '        classify(0)\n')

# 원본 코드를 고쳐야 하는 실패: 테스트는 맞고 코드가 0을 처리하지 않는다
CALC_NO_ZERO = CALC.replace('    if n == 0:\n        return "zero"\n', '')
SOURCE_TEST = BROKEN_TEST.replace('"Zero"', '"zero"')

ID_ZERO = "tests/test_calc.py::CalcTest::test_zero"
ID_NEG = "tests/test_calc.py::CalcTest::test_neg"
# unittest -v 줄: 3.11+ "test_x (mod.Class.test_x) ... ok", 3.9·3.10 "test_x (mod.Class) ... ok"
_LINE = re.compile(r"^(\w+) \(([\w.]+?)\.(\w+)(?:\.\1)?\)\s*\.\.\.\s*(ok|FAIL|ERROR)", re.M)


def fake_pytest(root, wt, d, ids=None, deadline=None, tb="no"):
    base = Path(wt) / (d if d not in (None, "", ".") else "")
    files = sorted({i.split("::")[0] for i in ids}) if ids else \
        sorted(str(p.relative_to(base)).replace("\\", "/") for p in base.rglob("test_*.py") if ".harness" not in p.parts)
    passed, failed, text = set(), set(), ""
    for f in files:
        if not (base / f).exists():
            return {"ran": False, "missing": True, "passed": set(), "failed": set(), "interrupted": None, "rc": 4, "output": ""}
        mod = f[:-3].replace("/", ".")
        r = subprocess.run([sys.executable, "-B", "-m", "unittest", "-v", mod], cwd=str(base), capture_output=True, text=True)
        text += r.stderr
        seen = False
        for name, _, cls, st in _LINE.findall(r.stderr):
            seen = True
            (passed if st == "ok" else failed).add("%s::%s::%s" % (f, cls, name))
        if not seen and r.returncode != 0:
            failed.add(f)  # 수집 오류
    if ids:
        keep = set(ids)
        passed = {p for p in passed if p in keep or p.split("::")[0] in keep}
        failed = {x for x in failed if x in keep or x.split("::")[0] in keep}
    return {"ran": True, "missing": False, "passed": passed, "failed": failed, "interrupted": None,
            "rc": 1 if failed else 0, "output": text}


class FakeClaude:
    def __init__(self, edits=None, ok=True):
        self.edits, self.ok, self.prompts = edits or {}, ok, []

    def __call__(self, root, prompt, cwd, tools, deadline=None, add_dirs=None):
        self.prompts.append(prompt)
        for rel, text in self.edits.items():
            p = Path(cwd) / rel
            if text is None:
                p.unlink()
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        return {"text": '{"done": true}', "tokens": 300, "ok": self.ok, "interrupted": None,
                "error": None if self.ok else "모델 오류"}


class TicketCase(LoopTestCase):
    def setUp(self):
        super().setUp()
        self.p_pytest = mock.patch("vmc_harness.loop.tickets.run_pytest", fake_pytest)
        self.p_pytest.start()

    def tearDown(self):
        self.p_pytest.stop()
        super().tearDown()

    def project(self, calc=CALC, test=BROKEN_TEST, failed=(ID_ZERO,)):
        self.write("calc.py", calc)
        self.write("tests/test_calc.py", test)
        self.write(".gitignore", ".harness/cache/\n")
        self.commit_old("init")
        self.facts([{"runner": "pytest", "dir": ".", "status": "fail", "failed": list(failed), "errors": [],
                     "collection_errors": []}])

    def facts(self, runs):
        write_json(self.root / ".harness/cache/facts.json",
                   {"generated_at": "2026-10-22T10:00:00+09:00",
                    "facts": {"test_baseline": {"value": runs, "command": "python -m pytest -q"}}})

    def repair_cand(self):
        from vmc_harness.loop import tickets
        self.assertEqual(tickets.sync_test_repairs(str(self.root)), 1)
        return [c for c in ledger.list_candidates(self.root) if c["kind"] == "test_repair"][0]

    def run_one(self, cand, fake):
        from vmc_harness.loop import tickets
        with mock.patch("vmc_harness.loop.cleanse.claude", fake):
            return tickets.process(str(self.root), cand)


class SyncTest(TicketCase):
    def test_baseline_failures_become_per_file_candidates(self):
        from vmc_harness.loop import tickets
        self.write("calc.py", CALC)
        self.write(".gitignore", ".harness/cache/\n")
        self.commit_old("init")
        write_json(self.root / ".harness/protected.json", {"paths": ["be/tests/test_audit.py"]})
        self.facts([{"runner": "pytest", "dir": "be", "status": "fail",
                     "failed": ["tests/test_a.py::test_1", "tests/test_a.py::test_2", "tests/test_audit.py::test_x"],
                     "errors": [], "collection_errors": [{"path": "tests/test_b.py", "reason": "ImportError"}]},
                    {"runner": "npm", "dir": "fe", "status": "fail"}])
        self.assertEqual(tickets.sync_test_repairs(str(self.root)), 2)  # a·b — 보호 영역 audit 제외, npm은 id 없음
        cs = {c["anchor"]["path"]: c for c in ledger.list_candidates(self.root)}
        self.assertEqual(set(cs), {"be/tests/test_a.py", "be/tests/test_b.py"})
        self.assertEqual(cs["be/tests/test_a.py"]["tests"], ["tests/test_a.py::test_1", "tests/test_a.py::test_2"])
        self.assertEqual(cs["be/tests/test_a.py"]["run_dir"], "be")
        self.assertEqual(tickets.sync_test_repairs(str(self.root)), 0)  # 다시 만들지 않는다

    def test_priority_repair_then_ticket_then_cleansing(self):
        from vmc_harness.loop import candidates, tickets
        self.project()
        candidates.accept(str(self.root), {"summary": "x", "change": "remove_unreferenced",
                                           "recheck": {"type": "ast", "query": "unreferenced calc.py::unused_helper"},
                                           "anchor": {"path": "calc.py", "symbol": "unused_helper"}})
        tickets.register_ticket(str(self.root), {"title": "구조 개선", "kind": "structure", "targets": ["calc.py"]})
        tickets.sync_test_repairs(str(self.root))
        self.assertEqual([c["kind"] for c in candidates.prioritized(str(self.root), scores={})],
                         ["test_repair", "ticket", "cleansing"])


class RepairTest(TicketCase):
    def test_test_only_fix_prepares_A_and_morning_merges(self):
        from vmc_harness.loop import merge
        self.project()
        c = self.repair_cand()
        r = self.run_one(c, FakeClaude({"tests/test_calc.py": FIXED_TEST}))
        self.assertEqual(r["outcome"], "prepared", r)
        self.assertEqual(r["grade"], "A")
        a = ledger.get_action(self.root, r["action_id"])
        self.assertEqual(a["basis"], {"type": "cleansing", "ref": c["candidate_id"]})
        self.assertEqual(a["change"], "repair_test")
        self.assertEqual(a["rerun"], {"dir": ".", "ids": [ID_ZERO]})
        self.assertEqual(a["target"], {"area": "tests/test_calc.py"})
        self.assertIn('"Zero"', (self.root / "tests/test_calc.py").read_text())  # 개발자 작업 트리 그대로
        out = merge.morning(str(self.root))
        self.assertEqual(out[0]["result"], "merged", out)
        self.assertIn('"zero"', (self.root / "tests/test_calc.py").read_text())
        self.assertEqual(ledger.get_candidate(self.root, c["candidate_id"])["status"], "resolved")
        self.assertTrue(any(n["kind"] == "cleansing_merged" for n in notices.pending(self.root)))

    def test_source_change_is_proposed_not_merged(self):
        from vmc_harness.loop import merge
        self.project(calc=CALC_NO_ZERO, test=SOURCE_TEST)
        c = self.repair_cand()
        r = self.run_one(c, FakeClaude({"calc.py": CALC}))
        self.assertEqual(r["outcome"], "proposed", r)
        cand = ledger.get_candidate(self.root, c["candidate_id"])
        self.assertEqual(cand["status"], "proposed")
        self.assertEqual(cand["proposed_reason"], "원본 코드 수정 필요")
        self.assertEqual(self.git("branch", "--list", cand["branch"]).stdout.strip(), cand["branch"])  # 브랜치는 남긴다
        n = [x for x in notices.pending(self.root) if x["kind"] == "loop_ticket_proposal"]
        self.assertEqual(len(n), 1)
        self.assertIn("대상 1개 통과 · 새 실패 0", n[0]["text"])
        self.assertEqual(ledger.list_actions(self.root), [])
        self.assertEqual(merge.morning(str(self.root)), [])

    def test_weakened_test_is_proposed(self):
        self.project()
        c = self.repair_cand()
        r = self.run_one(c, FakeClaude({"tests/test_calc.py": WEAK_TEST}))
        self.assertEqual(r["outcome"], "proposed", r)
        self.assertTrue(ledger.get_candidate(self.root, c["candidate_id"])["proposed_reason"].startswith("단언 수 감소"))

    def test_new_failure_fails_attempt(self):
        self.project(failed=(ID_ZERO,))
        c = self.repair_cand()
        broken = CALC.replace('return "neg"', 'return "NEG"')
        r = self.run_one(c, FakeClaude({"tests/test_calc.py": FIXED_TEST, "calc.py": broken}))
        self.assertEqual(r["outcome"], "failed", r)
        self.assertEqual(r["reason"], "새 실패 1개")

    def test_attempts_then_archived_and_not_retried(self):
        from vmc_harness.loop import candidates
        self.project()
        c = self.repair_cand()
        for i in range(3):
            r = self.run_one(ledger.get_candidate(self.root, c["candidate_id"]), FakeClaude({}))
        self.assertEqual(r["outcome"], "archived")
        cand = ledger.get_candidate(self.root, c["candidate_id"])
        self.assertEqual(cand["status"], "archived")
        self.assertEqual([a["reason"] for a in cand["attempts"]], ["바뀐 것 없음"] * 3)
        self.assertTrue((self.root / "tests/test_calc.py").exists())  # 지우지 않는다
        n = [x for x in notices.pending(self.root) if x["kind"] == "loop_ticket_archived"]
        self.assertEqual(len(n), 1)
        self.assertIn("3번 시도", n[0]["text"])
        self.assertEqual(candidates.prioritized(str(self.root), scores={}), [])
        from vmc_harness.loop import tickets
        self.assertEqual(tickets.sync_test_repairs(str(self.root)), 0)

    def test_auth_failure_not_counted(self):
        self.project()
        c = self.repair_cand()
        fake = FakeClaude({}, ok=False)
        with mock.patch("vmc_harness.loop.cleanse.claude",
                        lambda *a, **k: dict(fake(*a, **k), error="로그인 필요")):
            from vmc_harness.loop import tickets
            r = tickets.process(str(self.root), c)
        self.assertEqual(r["outcome"], "failed")
        self.assertEqual(ledger.get_candidate(self.root, c["candidate_id"])["attempts"], [])

    def test_protected_edit_rejected(self):
        self.project()
        write_json(self.root / ".harness/protected.json", {"paths": ["calc.py"]})
        c = self.repair_cand()
        r = self.run_one(c, FakeClaude({"tests/test_calc.py": FIXED_TEST, "calc.py": CALC + "\n# x\n"}))
        self.assertEqual(r["outcome"], "failed")
        self.assertTrue(r["reason"].startswith("보호 영역"))

    def test_deleting_test_rejected(self):
        self.project()
        c = self.repair_cand()
        r = self.run_one(c, FakeClaude({"tests/test_calc.py": None}))
        self.assertEqual(r["outcome"], "failed")

    def test_natural_resolution(self):
        self.project(test=FIXED_TEST)
        c = self.repair_cand()
        r = self.run_one(c, FakeClaude({}))
        self.assertEqual(r["outcome"], "resolved")


class DiagnosisTicketTest(TicketCase):
    def diagnosis(self):
        from vmc_harness.bootstrap import diagnose
        props = [
            {"no": 1, "title": "계산 모듈 테스트 보강", "kind": "structure", "evidence": "실패 3건", "blocks": "회귀",
             "scope": "calc.py", "cost": "1h", "targets": ["calc.py"]},
            {"no": 2, "title": "SQL은 패치 폴더에만", "kind": "gate", "evidence": "7건", "blocks": "DB 직접 변경",
             "scope": "*.sql", "cost": "0.1s", "condition": {"type": "command", "regex": "sqlcmd"}},
            {"no": 3, "title": "결재 경로 보호", "kind": "protected", "evidence": "2건", "blocks": "결재",
             "scope": "approval/", "cost": "-", "protected_globs": ["approval/**"]},
            {"no": 4, "title": "응답은 짧게", "kind": "rule", "evidence": "\"짧게\"", "blocks": "장황", "scope": "전체", "cost": "-"},
        ]
        write_json(diagnose.diagnosis_path(self.root), {"proposals": props, "status": "ok"})

    def test_structure_auto_ticket_others_wait(self):
        from vmc_harness.bootstrap import diagnose
        from vmc_harness.loop import tickets
        from vmc_harness.render import brief
        self.project(test=FIXED_TEST, failed=())
        self.diagnosis()
        self.assertEqual(tickets.sync_diagnosis(str(self.root)), 1)
        self.assertEqual(tickets.sync_diagnosis(str(self.root)), 0)
        ts = [c for c in ledger.list_candidates(self.root) if c["kind"] == "ticket"]
        self.assertEqual(len(ts), 1)
        self.assertEqual(ts[0]["targets"], ["calc.py"])
        self.assertNotIn("evidence", ts[0])  # 근거 원문은 장부에 넣지 않는다
        view = {v["id"]: v for v in brief.proposals_view(str(self.root))}
        self.assertEqual(view["1"]["buttons"], ["제외"])
        self.assertTrue(view["1"]["text"].startswith("루프가 처리할 일"))
        for n in ("2", "3", "4"):
            self.assertEqual(view[n]["buttons"], ["추가", "제외"])
        self.assertEqual(diagnose.accept(str(self.root), [1])[0]["skipped"], "이미 루프 티켓으로 등록됨")
        out = diagnose.exclude(str(self.root), [1])
        self.assertEqual(out[0]["ticket_status"], "excluded")
        self.assertTrue(out[0]["finding_id"])
        self.assertEqual(ledger.get_candidate(self.root, ts[0]["candidate_id"])["status"], "excluded")
        self.assertNotIn("1", {v["id"] for v in brief.proposals_view(str(self.root))})

    def test_ticket_tests_only_prepared_and_source_proposed(self):
        from vmc_harness.loop import tickets
        self.project(test=FIXED_TEST, failed=())
        self.diagnosis()
        tickets.sync_diagnosis(str(self.root))
        c = [x for x in ledger.list_candidates(self.root) if x["kind"] == "ticket"][0]
        extra = FIXED_TEST.replace("test_zero", "test_pos").replace("classify(0), \"zero\"", "classify(5), \"pos\"")
        fake = FakeClaude({"tests/test_calc_more.py": extra})
        r = self.run_one(c, fake)
        self.assertEqual(r["outcome"], "prepared", r)
        self.assertIn("계산 모듈 테스트 보강", fake.prompts[0])
        self.assertIn("실패 3건", fake.prompts[0])  # 근거는 로컬 진단 결과에서
        a = ledger.get_action(self.root, r["action_id"])
        self.assertEqual(a["change"], "ticket_tests")
        # 같은 영역 아닌 두 번째 티켓: 원본 코드 변경 → 제안
        c2 = tickets.register_ticket(str(self.root), {"title": "분기 정리", "kind": "structure", "targets": ["calc.py"]})
        r2 = self.run_one(c2, FakeClaude({"calc.py": CALC + "\n\ndef extra():\n    return 1\n"}))
        self.assertEqual(r2["outcome"], "proposed", r2)

    def test_cancel_prepared_ticket_discards(self):
        from vmc_harness.loop import tickets
        self.project(test=FIXED_TEST, failed=())
        c = tickets.register_ticket(str(self.root), {"title": "테스트 추가", "kind": "structure", "targets": ["calc.py"]})
        r = self.run_one(c, FakeClaude({"tests/test_more.py": FIXED_TEST}))
        self.assertEqual(r["outcome"], "prepared", r)
        br = ledger.get_action(self.root, r["action_id"])["branch"]
        tickets.cancel_ticket(str(self.root), c["candidate_id"])
        a = ledger.get_action(self.root, r["action_id"])
        self.assertEqual((a["status"], a["discarded"]), ("reverted", True))
        self.assertEqual(self.git("branch", "--list", br).stdout.strip(), "")


class TickDispatchTest(TicketCase):
    def test_run_syncs_and_dispatches(self):
        from vmc_harness.loop import tick
        self.project()
        seen = []

        def fake_ticket(root, cand, deadline=None):
            seen.append(cand["kind"])
            return {"outcome": "failed", "reason": "x", "tokens": 10}

        with mock.patch("vmc_harness.loop.tickets.process", fake_ticket), \
                mock.patch("vmc_harness.loop.cleanse.discover", return_value=(0, 0, None)):
            r = tick.run(str(self.root), "start-loop")
        self.assertTrue(r["ran"])
        self.assertEqual(seen, ["test_repair"])  # 같은 밤엔 한 번만
        from vmc_harness.core.jsonio import read_jsonl
        ev = [x for x in read_jsonl(tick.runs_path(str(self.root))) if x["event"] == "sync"]
        self.assertEqual(ev[0]["test_repair"], 1)
