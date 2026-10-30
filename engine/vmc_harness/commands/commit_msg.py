"""`vmc-harness commit-msg <메시지파일> [source] [sha]` — git prepare-commit-msg 훅에서 trailer를 붙인다(spec 03).

`--install` / `--uninstall`: 이 프로젝트에 prepare-commit-msg 훅 설치·해제(init·remove가 trailers.install_commit_hook을 부른다).
`--print`: 지금 붙을 줄만 출력.
"""

import argparse
import sys


def register(sub):
    p = sub.add_parser("commit-msg", help="커밋 trailer(Harness-Task 등) 붙이기 — git prepare-commit-msg 훅용")
    p.add_argument("msg_file", nargs="?")
    p.add_argument("source", nargs="?")
    p.add_argument("sha", nargs="?")
    p.add_argument("--install", action="store_true", help="prepare-commit-msg 훅 설치")
    p.add_argument("--uninstall", action="store_true", help="prepare-commit-msg 훅 해제")
    p.add_argument("--print", dest="print_only", action="store_true", help="붙일 줄만 출력")
    p.add_argument("--root", help=argparse.SUPPRESS)
    p.set_defaults(func=_run)


def _run(a):
    from vmc_harness.core import paths
    from vmc_harness.task import trailers
    root = a.root or paths.find_project_root()
    if not root:
        return 0  # 하네스 없는 레포의 커밋을 막지 않는다
    if a.install:
        h = trailers.install_commit_hook(root)
        print(str(h) if h else "git 레포 아님")
        return 0 if h else 2
    if a.uninstall:
        print("해제함" if trailers.uninstall_commit_hook(root) else "설치 안 됨")
        return 0
    if a.print_only or not a.msg_file:
        extra, tr = trailers.lines_for_commit(root, trailers.task_for_commit(root))
        print("\n".join(extra + tr))
        return 0
    try:
        trailers.prepare_commit_msg(root, a.msg_file, a.source)
    except Exception as e:  # 커밋을 막지 않는다
        print("vmc-harness commit-msg: %s" % e, file=sys.stderr)
    return 0
