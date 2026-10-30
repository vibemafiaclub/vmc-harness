"""bootstrap: 구조 지도·관례·보호 영역 후보 (합성 데이터)."""

import subprocess

from tests.util import HarnessTestCase
from vmc_harness.bootstrap import conventions, protected_suggest, structmap


def _domain(t, name, cls):
    t.write("be/core/%s/__init__.py" % name, "")
    t.write("be/core/%s/models.py" % name, "class %sRecord:\n    pass\n" % cls)
    t.write("be/core/%s/repository.py" % name,
            "from typing import Protocol\nfrom core.audit.models import AuditRecord\n"
            "class %sRepository(Protocol):\n    pass\n"
            "class InMemory%sRepository:\n    pass\n"
            "class Mssql%sRepository:\n    pass\n" % (cls, cls, cls))
    t.write("be/core/%s/service.py" % name, "from .repository import %sRepository\n\ndef run():\n    pass\n\ndef _hidden():\n    pass\n" % cls)


class MapTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        from vmc_harness.core.jsonio import write_json
        write_json(self.root / ".harness" / "config.json",
                   {"project_id": self.project_id, "profile": "standard", "source_roots": ["be", "fe"]})
        self.write("be/pyproject.toml", "[tool.pytest.ini_options]\ntestpaths=['tests']\n")
        self.write("be/core/__init__.py", "")
        _domain(self, "audit", "Audit")
        _domain(self, "orders", "Order")
        _domain(self, "users", "User")
        self.write("fe/package.json", '{"scripts":{"build":"next build","lint":"eslint ."}}')
        self.write("fe/tsconfig.json", '{"compilerOptions":{"baseUrl":".", // c\n "paths":{"@/*":["./src/*"]}}}')
        self.write("fe/src/lib/api.ts", "export function call() {}\nexport const X = 1\nexport default function main(){}\n")
        self.write("fe/src/app/page.tsx", "import { call } from '@/lib/api'\nimport x from '../lib/api'\nexport default function P(){}\n")
        self.write("fe/src/data/t.generated.ts", "export const T = 1\n")

    def test_build_surface_deps_module_of(self):
        data = structmap.build(self.root, use_llm=False)
        by = {m["id"]: m for m in data["modules"]}
        self.assertIn("be/core/orders", by)
        orders = by["be/core/orders"]
        self.assertIn("repository.OrderRepository", orders["surface"])
        self.assertIn("service.run", orders["surface"])
        self.assertNotIn("service._hidden", orders["surface"])
        self.assertIn("be/core/audit", orders["deps"])  # 절대 import(패키지 기준) 해석
        self.assertIn("fe/src/lib", by["fe/src/app"]["deps"])  # tsconfig 별칭 + 상대 경로
        self.assertIn("api.call", by["fe/src/lib"]["surface"])
        self.assertTrue((self.root / ".harness/cache/map.md").is_file())
        self.assertEqual(structmap.module_of(self.root, "be/core/orders/service.py"), "be/core/orders")
        self.assertIsNone(structmap.module_of(self.root, "docs/x.md"))
        # 프로젝트 밖 절대경로(세션 도구 호출에 남은 것)는 루트에서 멈춘다 — 무한 루프 회귀
        self.assertIsNone(structmap.module_of(self.root, "/Users/x/.claude/settings.json"))
        self.assertIsNone(structmap.module_of(self.root, "C:\\Users\\x\\a.py"))

    def test_risk_map_absolute_session_paths(self):
        from vmc_harness.bootstrap import report
        data = structmap.build(self.root, use_llm=False)
        sessions = [{"session_id": "s1", "human_prompts": [], "tool_calls": [
            {"write": True, "paths": ["/Users/x/.claude/settings.json", "C:\\Users\\x\\a.py",
                                      "be/core/orders/service.py"]}]}]
        rows = report.risk_map(self.root, data, {}, sessions, [])
        by = {r["id"]: r for r in rows}
        self.assertEqual(by["be/core/orders"]["edits"], 1)

    def test_diff_ignores_internal_change(self):
        old = structmap.build(self.root, use_llm=False)
        self.write("be/core/orders/service.py", "from .repository import OrderRepository\n\ndef run():\n    return 1  # 내부만\n\ndef _hidden():\n    pass\n")
        d0 = structmap.diff(old, structmap.build(self.root, use_llm=False))
        self.assertTrue(d0["empty"])  # 내부 구현만 바뀌면 지도 그대로
        self.write("be/core/orders/repository.py", "class OrderRepository:\n    pass\nclass InMemoryOrderRepository:\n    pass\nclass MssqlOrderRepository:\n    pass\n")
        new = structmap.build(self.root, use_llm=False)
        d = structmap.diff(old, new)
        self.assertEqual(d["added"], [])
        self.assertTrue(any(x["id"] == "be/core/orders" for x in d["deps_changed"]))  # import 하나 빠짐
        self.write("be/core/billing/__init__.py", "")
        self.write("be/core/billing/api.py", "def pay():\n    pass\n")
        d2 = structmap.diff(new, structmap.build(self.root, use_llm=False))
        self.assertEqual(d2["added"], ["be/core/billing"])

    def test_responsibility_kept_when_surface_same(self):
        data = structmap.build(self.root, use_llm=False)
        for m in data["modules"]:
            m["responsibility"] = "주문 처리"
        from vmc_harness.core.jsonio import write_json
        write_json(structmap.map_path(self.root), data)
        again = structmap.build(self.root, use_llm=False)
        self.assertTrue(all(m["responsibility"] == "주문 처리" for m in again["modules"]))

    def test_task_bundles(self):
        self.commit("base")
        self.write("be/core/pay/__init__.py", "")
        self.write("be/core/pay/a.py", "from core.payx.b import f\n")
        self.write("be/core/payx/__init__.py", "")
        self.write("be/core/payx/b.py", "def f():\n    pass\n")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "결제 추가\n\nHarness-Task: T-261028-abcdef"], cwd=self.root, check=True)
        structmap.build(self.root, use_llm=False)
        b = structmap.task_bundles(self.root)
        self.assertEqual(len(b), 1)
        self.assertEqual(sorted(b[0]["new_modules"]), ["be/core/pay", "be/core/payx"])
        self.assertIn(["be/core/pay", "be/core/payx"], b[0]["edges"])
        self.assertIn("모듈 2개 생김", b[0]["text"])
        self.assertIn("pay→payx", b[0]["text"])

    def test_conventions(self):
        data = structmap.build(self.root, use_llm=False)
        conv = conventions.detect(self.root, data)
        texts = " / ".join(c["text"] for c in conv)
        self.assertIn("InMemoryXRepository", texts)
        self.assertIn("MssqlXRepository", texts)
        self.assertTrue(any(c["kind"] == "file_set" and "repository.py" in c["files"] for c in conv))
        self.assertTrue(all(c.get("command") for c in conv))

    def test_protected_candidates(self):
        self.write("be/core/auth/reauth.py", "def f():\n    pass\n")
        self.write("SQL/SP/dbo.X_Approve.sql", "select 1")
        self.write("SQL/SP/dbo.Y_Approve.sql", "select 1")
        self.write("be/tests/audit/test_a.py", "def test():\n    pass\n")
        c = {x["glob"]: x for x in protected_suggest.suggest(self.root)}
        self.assertIn("be/core/audit/**", c)
        self.assertIn("be/core/auth/**", c)
        self.assertIn("SQL/SP/*Approve*", c)
        self.assertIn("fe/src/data/t.generated.ts", c)
        self.assertFalse(any(g.startswith("be/tests") for g in c))  # 테스트는 변조 금지 게이트 몫


class MapResponsibilityBudgetTest(HarnessTestCase):
    """책임 한 줄: 묶음 병렬 + 전체 상한 — 모듈 수천 개 PC에서 [5/7]이 수 시간 멈춘 것처럼 보이던 회귀."""

    def test_parallel_and_budget(self):
        import time
        from unittest import mock
        from vmc_harness.core import config, llm
        mods = [{"id": "m%02d" % i, "file_list": ["a.py"], "surface": [], "lang": ["python"], "lines": i} for i in range(12)]
        calls = []

        def fake(prompt, model=None, timeout=None, **_):
            calls.append(prompt)
            time.sleep(0.3)
            ids = [m["id"] for m in mods if '"%s"' % m["id"] in prompt]
            return {"text": "{%s}" % ", ".join('"%s": "일"' % i for i in ids)}

        orig = config.get
        over = {"bootstrap.map_llm_batch": 2, "bootstrap.map_llm_workers": 3, "bootstrap.map_budget_s": 0.1}
        said = []
        with mock.patch.object(llm, "available", return_value=True), mock.patch.object(llm, "nested", return_value=False), \
                mock.patch.object(llm, "claude_p", side_effect=fake), \
                mock.patch.object(config, "get", side_effect=lambda k, r=None, profile=None: over.get(k, orig(k, r))):
            t0 = time.time()
            out = structmap._llm_responsibilities(self.root, mods, progress=said.append)
            sec = time.time() - t0
        self.assertEqual(len(calls), 3)  # 상한 전에 낸 첫 묶음 3개(동시 3)만, 나머지 3개 묶음은 내지 않는다
        self.assertLess(sec, 0.9)  # 순차였다면 0.9초 이상
        self.assertEqual(set(out), {"m11", "m10", "m09", "m08", "m07", "m06"})  # 큰 모듈부터
        self.assertTrue(any("비워 둠" in s for s in said))
