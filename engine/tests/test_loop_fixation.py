"""루프: 고정 판정 ①~④(돌연변이 점검 포함)과 머지 등급 A/B/C."""

from tests.test_loop_util import CALC_REFACTORED, PIN_TEST, WEAK_TEST, LoopTestCase


class FixationTest(LoopTestCase):
    def _build(self, test_text):
        base = self.make_calc_project()
        self.write("tests/test_calc.py", test_text)
        pin = self.commit_old("pin")
        self.write("calc.py", CALC_REFACTORED)
        change = self.commit_old("refactor")
        return base, pin, change

    def test_strong_pin_passes_all(self):
        from vmc_harness.loop import fixation
        base, pin, change = self._build(PIN_TEST)
        self.qa_pass("calc-page", ["calc.py"])
        r = fixation.check(str(self.root), str(self.root), base, pin, change)
        self.assertTrue(r["1"], r["detail"])
        self.assertTrue(r["2"], r["detail"])
        self.assertTrue(r["3"], r["detail"])
        self.assertTrue(r["4"], r["detail"])
        self.assertTrue(r["ok"])
        self.assertGreater(r["detail"]["mutation"]["total"], 0)
        self.assertEqual(self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "main")

    def test_weak_pin_fails_coverage_and_mutation(self):
        from vmc_harness.loop import fixation
        base, pin, change = self._build(WEAK_TEST)
        self.qa_pass("calc-page", ["calc.py"])
        r = fixation.check(str(self.root), str(self.root), base, pin, change)
        self.assertTrue(r["1"])
        self.assertFalse(r["2"])  # 바뀐 줄이 안 덮임
        self.assertFalse(r["3"])  # 망가뜨려도 테스트가 안 깨짐
        self.assertFalse(r["ok"])

    def test_no_qa_scenario_fails_4(self):
        from vmc_harness.loop import fixation
        base, pin, change = self._build(PIN_TEST)
        r = fixation.check(str(self.root), str(self.root), base, pin, change)
        self.assertTrue(r["3"])
        self.assertFalse(r["4"])
        self.assertIn("QA 시나리오 없음", r["detail"]["4"])

    def test_pin_must_come_first(self):
        from vmc_harness.loop import fixation
        base = self.make_calc_project()
        self.write("tests/test_calc.py", PIN_TEST)
        self.write("calc.py", CALC_REFACTORED)
        both = self.commit_old("both")
        r = fixation.check(str(self.root), str(self.root), base, both, both)
        self.assertFalse(r["1"])

    def test_text_mutants(self):
        from vmc_harness.loop import fixation
        src = "export function f(a, b) {\n  if (a === b && a > 0) {\n    return true;\n  }\n  return false;\n}\n"
        ms = fixation.mutants_for(src, "a.ts", {2, 3}, 10)
        self.assertTrue(ms)
        self.assertTrue(all(m[1] != src for m in ms))
        self.assertTrue(any("!==" in m[1] for m in ms))


class GradeTest(LoopTestCase):
    def test_grade_A_comment_only(self):
        from vmc_harness.loop import merge
        base = self.make_calc_project()
        self.write("calc.py", "# 분류\n" + (self.root / "calc.py").read_text(encoding="utf-8").replace("    if n < 0:", "    if n < 0:  # 음수"))
        change = self.commit_old("comment")
        g = merge.grade(str(self.root), str(self.root), base, None, change, {"anchor": {"path": "calc.py"}}, "comment_format")
        self.assertEqual(g["grade"], "A", g)

    def test_grade_A_remove_unreferenced(self):
        from vmc_harness.loop import merge
        base = self.make_calc_project()
        text = (self.root / "calc.py").read_text(encoding="utf-8")
        self.write("calc.py", text.split("\n\ndef unused_helper")[0] + "\n")
        change = self.commit_old("remove")
        g = merge.grade(str(self.root), str(self.root), base, None, change,
                        {"anchor": {"path": "calc.py", "symbol": "unused_helper"}}, "remove_unreferenced")
        self.assertEqual(g["grade"], "A", g)

    def test_new_file_or_protected_not_auto(self):
        from vmc_harness.core.jsonio import write_json
        from vmc_harness.loop import merge
        base = self.make_calc_project()
        self.write("helpers.py", "def h():\n    return 1\n")
        change = self.commit_old("new file")
        g = merge.grade(str(self.root), str(self.root), base, None, change, {"anchor": {"path": "calc.py"}}, "internal_cleanup")
        self.assertIsNone(g["grade"])
        self.assertEqual(g["reason"], "새 파일")
        write_json(self.root / ".harness/protected.json", {"paths": [{"glob": "calc.py", "reason": "시험"}]})
        self.write("calc.py", "# x\n" + (self.root / "calc.py").read_text(encoding="utf-8"))
        c2 = self.commit_old("touch protected")
        g = merge.grade(str(self.root), str(self.root), change, None, c2, {"anchor": {"path": "calc.py"}}, "comment_format")
        self.assertEqual(g["reason"], "보호 영역")

    def test_write_path_is_C(self):
        from vmc_harness.loop import merge
        self.write("repo.py", "def save(cur, x):\n    cur.execute('INSERT INTO t VALUES (?)', x)\n")
        base = self.commit_old("init")
        self.write("repo.py", "def save(cur, x):\n    q = 'INSERT INTO t VALUES (?)'\n    cur.execute(q, x)\n")
        change = self.commit_old("tidy")
        g = merge.grade(str(self.root), str(self.root), base, None, change,
                        {"anchor": {"path": "repo.py", "symbol": "save"}}, "internal_cleanup")
        self.assertEqual(g["grade"], "C")

    def test_grade_B_full(self):
        from vmc_harness.loop import merge
        base = self.make_calc_project()
        self.write("tests/test_calc.py", PIN_TEST)
        pin = self.commit_old("pin")
        self.write("calc.py", CALC_REFACTORED)
        change = self.commit_old("refactor")
        self.qa_pass("calc-page", ["calc.py"])
        g = merge.grade(str(self.root), str(self.root), base, pin, change,
                        {"anchor": {"path": "calc.py", "symbol": "classify"}}, "internal_cleanup")
        self.assertEqual(g["grade"], "B", g)
