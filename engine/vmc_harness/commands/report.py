"""`vmc-harness report` — 완료 보고 맨 위에 붙일 블록(범위 밖 변경 → 영향 → 증거 → 승인)을 출력(spec 03).

`--why <경로> "<이유>"`: 범위를 넓힌 이유 한 줄(선택).
"""

import argparse
import json
import sys


def register(sub):
    p = sub.add_parser("report", help="완료 보고 맨 위 블록 출력(범위 밖 변경·영향·증거·승인)")
    p.add_argument("--why", nargs=2, metavar=("PATH", "REASON"), action="append",
                   help="범위 밖 변경 이유 한 줄(선택)")
    p.add_argument("--task", help="작업 id(없으면 지금 세션 작업)")
    p.add_argument("--session")
    p.add_argument("--json", action="store_true")
    p.add_argument("--root", help=argparse.SUPPRESS)
    p.set_defaults(func=_run)


def _run(a):
    from vmc_harness.core import paths
    from vmc_harness.task import report, state as st
    root = a.root or paths.find_project_root()
    if not root:
        print("하네스가 설치되지 않은 프로젝트", file=sys.stderr)
        return 2
    task = st.by_id(root, a.task) if a.task else st.current(root, st.resolve_session_id(root, a.session))
    if not task:
        print("착수 선언 없음", file=sys.stderr)
        return 2
    for path, reason in a.why or []:
        task = report.record_reason(root, task, path, reason)
    rec = report.build(root, task, allow_llm=True)
    if a.json:
        print(json.dumps({k: rec[k] for k in ("task_id", "text", "out_of_scope", "modules", "approvals")},
                         ensure_ascii=False, indent=2))
    else:
        print(rec["text"])
    return 0
