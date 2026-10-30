"""루프: 검출 도구 · 후보(재확인 조건·앵커·해시·우선순위) · 낡음 처리."""

import os
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.test_loop_util import LoopTestCase
from vmc_harness.core import ledger


class DetectTest(LoopTestCase):
    def test_unreferenced_duplicates_giant(self):
        from vmc_harness.loop import detect
        self.make_calc_project()
        self.write("main.py", "from calc import classify\nprint(classify(1))\n")
        body = "\n".join("    total += xs[%d] * %d" % (i, i) for i in range(8))
        self.write("dup.py", "def a(xs):\n    total = 0\n%s\n    return total\n\n\ndef b(ys):\n    total = 0\n%s\n    return total\n"
                   % (body, body.replace("xs[", "ys[").replace("* ", "* 9")))
        self.write("orphan.py", "def lonely():\n    return 1\n")
        big = "def huge():\n" + "".join("    x%d = %d\n" % (i, i) for i in range(120)) + "    return 0\n"
        self.write("big.py", big)
        self.commit_old("more")
        un = detect.unreferenced(str(self.root))
        syms = {(u["path"], u.get("symbol")) for u in un}
        self.assertIn(("calc.py", "unused_helper"), syms)
        self.assertNotIn(("calc.py", "classify"), syms)
        self.assertIn(("orphan.py", None), syms)
        dups = detect.duplicates(str(self.root))
        self.assertTrue(any({m["symbol"] for m in g["members"]} == {"a", "b"} for g in dups), dups)
        g = detect.giant(str(self.root))
        self.assertTrue(any(x["symbol"] == "huge" for x in g))

    def test_wander_score(self):
        from vmc_harness.loop import detect
        s = {"a/x.py": 10, "a/y.py": 4, "b/z.py": 2}
        self.assertGreater(detect.wander_score(s, "a/x.py"), detect.wander_score(s, "b/z.py"))
        self.assertGreater(detect.wander_score(s, "a/new.py"), 0)  # 같은 폴더에서 헤맨 흔적

    def test_ts_scan_with_customer_typescript(self):
        """고객 node_modules/typescript를 쓰는 내장 node 스크립트. VMC_TEST_TS_NODE_MODULES가 없으면 건너뛴다."""
        from vmc_harness.loop import detect
        nm = os.environ.get("VMC_TEST_TS_NODE_MODULES")
        if not nm or not shutil.which("node") or not (Path(nm) / "typescript").is_dir():
            raise unittest.SkipTest("typescript 없음")
        (self.root / "node_modules").mkdir()
        os.symlink(str(Path(nm) / "typescript"), str(self.root / "node_modules" / "typescript"))
        self.write(".gitignore", "node_modules\n")
        self.write("src/util.ts", "export function used(a: number) { return a + 1; }\nexport function dead() { return 2; }\n")
        self.write("src/orphan.ts", "export const z = 1;\n")
        self.write("src/main.ts", "import { used } from './util';\nconsole.log(used(1));\n")
        self.commit_old("ts")
        res = detect.ts_scan(str(self.root))
        self.assertNotIn("error", res, res)
        un = {(u["path"], u["symbol"]) for u in res["unreferenced"]}
        self.assertIn(("src/util.ts", "dead"), un)
        self.assertNotIn(("src/util.ts", "used"), un)
        self.assertIn(("src/orphan.ts", None), un)
        self.assertTrue(any(f["symbol"] == "used" for f in res["functions"]))


class CandidateTest(LoopTestCase):
    def prop(self, **over):
        p = {"summary": "안 쓰는 함수 제거", "change": "remove_unreferenced",
             "recheck": {"type": "ast", "query": "unreferenced calc.py::unused_helper"},
             "anchor": {"path": "calc.py", "symbol": "unused_helper"}}
        p.update(over)
        return p

    def test_accept_requires_true_recheck(self):
        from vmc_harness.loop import candidates
        self.make_calc_project()
        self.write("main.py", "from calc import classify\n")
        self.commit_old("main")
        rec, why = candidates.accept(str(self.root), self.prop(recheck={"type": "ast", "query": "unreferenced calc.py::classify"},
                                                                anchor={"path": "calc.py", "symbol": "classify"}))
        self.assertIsNone(rec)
        self.assertEqual(why, "재확인 조건 거짓")
        self.assertIsNone(candidates.accept(str(self.root), self.prop(anchor={"path": "calc.py:9"}))[0])
        self.assertIsNone(candidates.accept(str(self.root), self.prop(recheck={"type": "grep", "query": "a\nb"}))[0])
        rec, why = candidates.accept(str(self.root), self.prop())
        self.assertIsNotNone(rec, why)
        self.assertEqual(rec["kind"], "cleansing")
        self.assertEqual(rec["anchor"], {"path": "calc.py", "symbol": "unused_helper"})
        self.assertTrue(rec["target_hash"])
        self.assertTrue(rec["created_commit"])
        self.assertEqual(rec["change"], "remove_unreferenced")
        rec2, _ = candidates.accept(str(self.root), self.prop())  # 같은 후보는 count만
        self.assertEqual(rec2["count"], 2)
        self.assertEqual(len(ledger.list_candidates(self.root)), 1)

    def test_grep_recheck_and_protected_and_write_path(self):
        from vmc_harness.core.jsonio import write_json
        from vmc_harness.loop import candidates
        self.make_calc_project()
        self.write("repo.py", "def save(cur):\n    cur.execute('DELETE FROM t')\n")
        self.commit_old("repo")
        self.assertTrue(candidates.run_recheck(str(self.root), {"type": "grep", "query": r"def classify"}))
        self.assertTrue(candidates.run_recheck(str(self.root), {"type": "grep", "query": r"def nope", "expect": "none"}))
        rec, _ = candidates.accept(str(self.root), self.prop(change="internal_cleanup", summary="정리",
                                                             recheck={"type": "ast", "query": "defined repo.py::save"},
                                                             anchor={"path": "repo.py", "symbol": "save"}))
        self.assertEqual(rec["change"], "write_path")  # 쓰기 경로 → 제안만
        write_json(self.root / ".harness/protected.json", ["calc.py"])
        self.assertEqual(candidates.accept(str(self.root), self.prop())[1], "보호 영역")

    def test_refresh_natural_resolution_and_expiry(self):
        from vmc_harness.loop import candidates
        self.make_calc_project()
        rec, _ = candidates.accept(str(self.root), self.prop())
        self.write("main.py", "from calc import unused_helper\n")
        self.commit_old("use it")
        self.assertEqual(candidates.refresh(str(self.root), rec, str(self.root)), "resolved")
        self.assertEqual(ledger.list_candidates(self.root)[0]["status"], "resolved")
        # 만료: 앵커 파일에 사람 커밋이 쌓이면
        os.remove(str(self.root / "main.py"))
        self.commit_old("unuse")
        rec, _ = candidates.accept(str(self.root), self.prop())
        for i in range(3):
            self.write("calc.py", (self.root / "calc.py").read_text(encoding="utf-8") + "# %d\n" % i)
            self.commit_old("human %d" % i)
        self.assertTrue(candidates.expire_if_stale(str(self.root), rec, "main"))

    def test_priority_by_wander(self):
        from vmc_harness.loop import candidates
        self.make_calc_project()
        self.write("other.py", "def lonely():\n    return 1\n")
        self.commit_old("o")
        a, _ = candidates.accept(str(self.root), self.prop())
        b, _ = candidates.accept(str(self.root), self.prop(recheck={"type": "ast", "query": "unreferenced other.py::lonely"},
                                                           anchor={"path": "other.py", "symbol": "lonely"}))
        order = [c["candidate_id"] for c in candidates.prioritized(str(self.root), scores={"other.py": 9})]
        self.assertEqual(order[0], b["candidate_id"])

    def test_busy_files(self):
        from vmc_harness.loop import candidates
        self.make_calc_project()
        self.write("fresh.py", "x = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "now"], cwd=self.root, check=True)  # 지금 사람 커밋
        self.git("checkout", "-q", "-b", "feature")
        self.write("feat.py", "y = 2\n")
        self.commit_old("feat")
        self.git("checkout", "-q", "main")
        busy = candidates.busy_files(str(self.root), "main")
        self.assertIn("fresh.py", busy)
        self.assertIn("feat.py", busy)
        self.assertNotIn("calc.py", busy)
