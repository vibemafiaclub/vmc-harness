"""커널 CI — 설정 키 메타 (spec 11).

- 모든 설정 키에 주석 메타 `unit=`·`md=`(model_dependent yes|no)·`spec=`이 있다
- 프로파일 덮어쓰기(`profile.<이름>.<키>`)는 메타를 원래 키에서 물려받는다 — 원래 키가 있어야 한다(오타 방지)
- md=yes 키 목록을 출력한다(모델 교체 때 다시 잴 값 — 회귀 세트와 함께 본다): `python3 -m tests.test_kernel_config`
"""

import re
import unittest

from vmc_harness.core import config

_MD = re.compile(r"\bmd=(yes|no)\b")


def model_dependent_keys():
    values, meta = config.defaults()
    return sorted(k for k in values if not k.startswith("profile.") and _MD.search(meta.get(k, "") or "")
                  and _MD.search(meta[k]).group(1) == "yes")


class KernelConfigTest(unittest.TestCase):
    def test_every_key_has_meta(self):
        values, meta = config.defaults()
        bad = []
        for k in values:
            if k.startswith("profile."):
                continue
            m = meta.get(k, "") or ""
            lacks = [x for x in ("unit=", "spec=") if x not in m]
            if not _MD.search(m):
                lacks.append("md=yes|no")
            if lacks:
                bad.append("%s: %s 없음" % (k, ", ".join(lacks)))
        self.assertEqual(bad, [], "\n".join(bad))

    def test_profile_overrides_point_to_real_keys(self):
        values, _ = config.defaults()
        bad = []
        for k in values:
            if not k.startswith("profile."):
                continue
            parts = k.split(".", 2)
            if len(parts) < 3 or parts[1] not in config.PROFILES or parts[2] not in values:
                bad.append(k)
        self.assertEqual(bad, [], "원래 키가 없는 프로파일 덮어쓰기: %s" % bad)

    def test_model_dependent_listed(self):
        keys = model_dependent_keys()
        self.assertIn("task.small_max_p50_sec", keys)   # spec 11 표의 **예** 항목
        self.assertIn("budget.small_task_overhead_s", keys)


if __name__ == "__main__":
    values, meta = config.defaults()
    print("md=yes 설정 키 (%d개) — 모델을 바꾸면 회귀 세트와 함께 다시 잰다" % len(model_dependent_keys()))
    for k in model_dependent_keys():
        print("  %-40s = %-12r %s" % (k, values[k], meta.get(k, "")))
