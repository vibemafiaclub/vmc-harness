"""커널 CI — 주입 허용 목록(spec 01 P5).

검사 본체는 이미 다른 영역 테스트에 있다(중복하지 않는다). 이 파일은 그 커널 테스트가 사라지지 않았는지만 본다.
- 허용 목록·숫자 압박 거르기: tests/test_core.py CoreTest.test_inject_allowlist (core.inject.message)
- 디스패처가 허용 목록 밖을 버림 + P80을 넘기는 데모에서 작업 중 숫자 주입 0건: tests/test_gates_dispatch.py
"""

import importlib
import unittest

REQUIRED = [
    ("tests.test_core", "test_inject_allowlist"),
    ("tests.test_gates_dispatch", "test_p80_demo_zero_numeric_injection_during_work"),
]


# 변수로 넘기는 사유가 바로 위에서 inject.message()로 만들어지는 곳(파일 단위 허용 — 새 곳은 inject.message() 호출을 그 자리에)
ALLOWED_NAME_REASON = {"gates": ("common.py",), "task": ("handlers.py",)}


class KernelInjectRefTest(unittest.TestCase):
    def test_kernel_inject_tests_exist(self):
        missing = []
        for mod, name in REQUIRED:
            m = importlib.import_module(mod)
            found = any(isinstance(c, type) and issubclass(c, unittest.TestCase) and hasattr(c, name)
                        for c in vars(m).values())
            if not found:
                missing.append("%s.%s" % (mod, name))
        self.assertEqual(missing, [], "01 P5 커널 테스트가 없어졌다")

    def test_block_reasons_built_by_inject(self):
        """막는 결과(HookResult(block=True))의 사유는 core.inject.message()나 그걸 쓰는 gates.common.block()으로만 만든다.
        꼬리표 없는 사유는 디스패처가 고정 문구로 바꿔 무엇을 어겼는지가 사라진다(현장 결함 C8 — db·qa 차단 7건)."""
        import ast
        from pathlib import Path
        import vmc_harness
        bad = []
        for f in Path(vmc_harness.__file__).parent.rglob("*.py"):
            tree = ast.parse(f.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "HookResult"):
                    continue
                kw = {k.arg: k.value for k in node.keywords}
                if not (isinstance(kw.get("block"), ast.Constant) and kw["block"].value is True):
                    continue
                r = kw.get("reason")
                ok = (isinstance(r, ast.Call) and getattr(r.func, "attr", None) == "message") or (
                    isinstance(r, ast.Name) and f.name in ALLOWED_NAME_REASON.get(f.parent.name, ()))
                if not ok:
                    bad.append("%s:%d" % (f.relative_to(Path(vmc_harness.__file__).parent), node.lineno))
        self.assertEqual(bad, [], "막는 사유를 inject.message()로 만들지 않은 곳")

    def test_allowlist_is_spec_list(self):
        from vmc_harness.core import inject
        # 01 P5: 안전 게이트 위반 · 종료 시점 빠진 증거 · PM 의견 (+ 착수 시점 정보 · 사람이 쓴 용어 뜻)
        self.assertEqual(set(inject.ALLOWED), {"gate_violation", "missing_evidence", "pm_opinion", "declare_context",
                                               "term_context"})


if __name__ == "__main__":
    unittest.main()
