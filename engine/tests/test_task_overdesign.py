import json
import sys
import types
from unittest import mock

from tests.util import HarnessTestCase

from vmc_harness.task import declare as dec, overdesign, scope, state as st

DUP = '''def total_price(items, rate):
    total = 0
    for it in items:
        if it.qty > 0:
            total += it.price * it.qty
    if rate:
        total = total * (1 + rate)
    return round(total, 2)
'''


class OverdesignTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        self.write("app/billing.py", DUP + "\n\ndef use():\n    return total_price([], 0)\n")
        self.write("package.json", json.dumps({"dependencies": {"react": "1"}}))
        self.write("requirements.txt", "requests==2\n")
        self.write("pyproject.toml", '[project]\nname = "x"\ndependencies = ["flask>=2", "pkg[extra]>=1"]\n')
        self.commit()
        st.session_touch(self.root, "S1", "claude")
        patcher = mock.patch("vmc_harness.task.pm.start", return_value="sync")  # 실제 claude -p를 띄우지 않는다
        patcher.start()
        self.addCleanup(patcher.stop)

    def _task(self, **kw):
        d = {"intent": "청구 금액 계산 수정", "scope": ["app/billing.py"],
             "predict": {"tokens": [1, 2, 3], "agent_sec": [60, 120, 170]}}
        d.update(kw)
        return dec.declare(self.root, d)["task"]

    def _kinds(self, task):
        return {(s["kind"], s.get("name") or s.get("path")) for s in overdesign.machine_signals(self.root, task)}

    def test_new_file_and_module(self):
        t = self._task()
        self.write("app/newpkg/helpers.py", "def _x():\n    return 1\n")
        k = self._kinds(t)
        self.assertIn(("new_file_out_of_scope", "app/newpkg/helpers.py"), k)
        self.assertIn(("new_module_out_of_scope", "app/newpkg"), k)
        t2 = self._task(new=["app/newpkg/"])
        self.assertEqual({x for x in self._kinds(t2) if x[0].startswith("new_")}, set())

    def test_new_dependency(self):
        t = self._task()
        self.write("package.json", json.dumps({"dependencies": {"react": "1", "lodash": "4"}}))
        self.write("requirements.txt", "requests==2\nhttpx>=0.1  # comment\n")
        self.write("pyproject.toml", '[project]\nname = "x"\ndependencies = ["flask>=2", "pkg[extra]>=1", "pydantic"]\n')
        k = self._kinds(t)
        for name in ("lodash", "httpx", "pydantic"):
            self.assertIn(("new_dependency", name), k)
        self.assertNotIn(("new_dependency", "react"), k)
        t2 = self._task(new=["lodash", "httpx", "pydantic"])
        self.assertFalse([x for x in self._kinds(t2) if x[0] == "new_dependency"])

    def test_unreferenced_public(self):
        t = self._task()
        self.write("app/billing.py", DUP + "\n\ndef use():\n    return total_price([], 0)\n\n"
                                         "def export_csv(rows):\n    return rows\n\n"
                                         "def used_one():\n    return 1\n\nX = used_one()\n")
        self.write("web/a.ts", "export function formatWon(n: number) { return n }\nexport const GET = 1\n")
        k = self._kinds(t)
        self.assertIn(("unreferenced_public", "export_csv"), k)
        self.assertNotIn(("unreferenced_public", "used_one"), k)
        self.assertIn(("unreferenced_public", "formatWon"), k)
        self.assertNotIn(("unreferenced_public", "GET"), k)

    def test_single_impl_abstraction_and_convention(self):
        t = self._task()
        self.write("app/billing.py", DUP + "\n\ndef use():\n    return total_price([], 0)\n\n"
                                         "from typing import Protocol\n\n"
                                         "class _PriceRepo(Protocol):\n    def get(self): ...\n\n"
                                         "class MssqlPriceRepo:\n    def get(self):\n        return 1\n\n"
                                         "R = MssqlPriceRepo()\n")
        self.assertIn(("single_impl_abstraction", "_PriceRepo"), self._kinds(t))
        fake = types.ModuleType("vmc_harness.bootstrap.facts")
        fake.load = lambda root: {"conventions": ["도메인마다 Protocol + InMemory + Mssql 3종 세트"]}
        pkg = types.ModuleType("vmc_harness.bootstrap")
        saved = {k: sys.modules.get(k) for k in ("vmc_harness.bootstrap", "vmc_harness.bootstrap.facts")}
        sys.modules["vmc_harness.bootstrap"] = sys.modules.get("vmc_harness.bootstrap") or pkg
        sys.modules["vmc_harness.bootstrap.facts"] = fake
        try:
            self.assertNotIn(("single_impl_abstraction", "_PriceRepo"), self._kinds(t))
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v
        self.write("web/b.ts", "export interface Store { get(): number }\nclass A implements Store { get() { return 1 } }\n"
                               "export const s = new A()\nconsole.log(s)\n")
        self.assertIn(("single_impl_abstraction", "Store"), self._kinds(t))

    def test_near_duplicate(self):
        t = self._task()
        copy = DUP.replace("total_price", "sum_cost").replace("items", "lines").replace("total", "acc")
        self.write("app/billing.py", DUP + "\n\ndef use():\n    return total_price([], 0) + sum_cost([], 0)\n\n" + copy)
        k = self._kinds(t)
        self.assertIn(("near_duplicate_function", "sum_cost"), k)

    def test_describe_has_no_numbers_pressure(self):
        text = overdesign.describe([{"kind": "new_dependency", "path": "package.json", "name": "lodash"}])
        self.assertEqual(text, "과설계 신호: 새 의존성 lodash(package.json)")

    def test_scope_matching(self):
        d = {"scope": ["src/", "web/*.tsx", "app/billing.py"], "new": ["lib/new_mod"], "test_change": None}
        self.assertTrue(scope.in_scope(self.root, d, "src/a/b.py"))
        self.assertTrue(scope.in_scope(self.root, d, "web/x.tsx"))
        self.assertTrue(scope.in_scope(self.root, d, "lib/new_mod/a.py"))
        self.assertFalse(scope.in_scope(self.root, d, "srcx/a.py"))
        self.assertFalse(scope.in_scope(self.root, d, "app/other.py"))
