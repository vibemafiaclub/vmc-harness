"""ledger·brain 명령 공용(명령 모듈이 아님 — register 없음)."""

import sys

from vmc_harness.core import paths


def need_root():
    root = paths.find_project_root()
    if root is None:
        print("하네스가 설치된 프로젝트가 아님(.harness/config.json 없음)", file=sys.stderr)
    return root
