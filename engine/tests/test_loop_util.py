"""루프 테스트 공용: 합성 Python 프로젝트(과거 날짜 커밋 — 최근 사람 커밋 제외 규칙에 안 걸리게)."""

import os
import subprocess

from tests.util import HarnessTestCase

CALC = '''def classify(n):
    if n < 0:
        return "neg"
    if n == 0:
        return "zero"
    return "pos"


def unused_helper(x):
    return x * 2
'''

CALC_REFACTORED = '''def classify(n):
    if n < 0:
        return "neg"
    return "zero" if n == 0 else "pos"


def unused_helper(x):
    return x * 2
'''

PIN_TEST = '''import unittest

from calc import classify


class PinTest(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify(-3), "neg")
        self.assertEqual(classify(0), "zero")
        self.assertEqual(classify(1), "pos")
        self.assertEqual(classify(-1), "neg")


if __name__ == "__main__":
    unittest.main()
'''

WEAK_TEST = '''import unittest

from calc import classify


class WeakTest(unittest.TestCase):
    def test_neg(self):
        self.assertEqual(classify(-3), "neg")
'''


class LoopTestCase(HarnessTestCase):
    OLD_DATE = "2020-01-01T10:00:00+09:00"

    def commit_old(self, msg="c", date=None, cwd=None):
        env = dict(os.environ)
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date or self.OLD_DATE
        cwd = str(cwd or self.root)
        subprocess.run(["git", "add", "-A"], cwd=cwd, check=True)
        subprocess.run(["git", "commit", "-q", "-m", msg, "--allow-empty"], cwd=cwd, check=True, env=env)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True).stdout.strip()

    def make_calc_project(self):
        self.write("calc.py", CALC)
        self.write(".gitignore", ".harness/cache/\n")
        return self.commit_old("init")

    def git(self, *args, cwd=None):
        return subprocess.run(["git"] + list(args), cwd=str(cwd or self.root), capture_output=True, text=True)

    def qa_pass(self, sid, paths):
        from vmc_harness.qa import results, scenarios
        scenarios.save(self.root, {"id": sid, "title": sid, "paths": paths, "expectations": [
            {"text": "분류가 맞다", "source": {"type": "current_behavior", "ref": "calc.py::classify"}}]})
        ev = self.write(".harness/cache/evidence/T-x/%s.png" % sid, "png")
        results.begin(self.root, sid)
        results.set_status(self.root, sid, "PASS", evidence=[str(ev)])
