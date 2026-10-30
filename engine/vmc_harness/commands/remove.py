"""`vmc-harness remove` — 훅·MCP·스케줄러·표시 블록·commit hook 원상복구, 로컬 원본 삭제 (spec 10)."""

import sys

from vmc_harness.core import paths


def register(sub):
    p = sub.add_parser("remove", help="이 프로젝트에서 하네스 설치를 원상복구(.harness/는 기본 남김)")
    p.add_argument("--purge-harness", action="store_true", help=".harness/(팀 장부·게이트·규칙)까지 지운다")
    p.set_defaults(func=run)


def run(args):
    root = paths.find_project_root()
    if root is None:
        print("하네스가 설치된 프로젝트가 아님(.harness/config.json 없음)", file=sys.stderr)
        return 2
    from vmc_harness.bootstrap import remove
    remove.run(root, purge_harness=args.purge_harness)
    return 0
