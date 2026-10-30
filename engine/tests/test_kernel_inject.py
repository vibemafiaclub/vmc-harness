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

    def test_allowlist_is_spec_list(self):
        from vmc_harness.core import inject
        # 01 P5: 안전 게이트 위반 · 종료 시점 빠진 증거 · PM 의견 (+ 착수 시점 정보 · 사람이 쓴 용어 뜻)
        self.assertEqual(set(inject.ALLOWED), {"gate_violation", "missing_evidence", "pm_opinion", "declare_context",
                                               "term_context"})


if __name__ == "__main__":
    unittest.main()
