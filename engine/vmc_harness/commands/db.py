"""`vmc-harness db query "<SQL>"` · `db dryrun <파일>` · `db approval <파일>` · `db facts` (spec 02·10)."""

import json
import os
import sys


def register(sub):
    p = sub.add_parser("db", help="개발 DB 조회(SELECT만)·SQL 패치 드라이런")
    dsub = p.add_subparsers(dest="db_command")
    q = dsub.add_parser("query", help="SELECT 하나 실행(읽기 전용 계정·타임아웃·행 상한)")
    q.add_argument("sql")
    q.add_argument("--json", action="store_true")
    q.set_defaults(func=_query)
    d = dsub.add_parser("dryrun", help="SQL 패치를 개발/복제 DB 트랜잭션 안에서 실행 후 롤백, 영향 행 수만")
    d.add_argument("path")
    d.add_argument("--task", help="작업 id(없으면 현재 작업)")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=_dryrun)
    a = dsub.add_parser("approval", help="운영 적용 승인 기록 만들기(롤백 SQL·드라이런 결과 필수)")
    a.add_argument("path")
    a.add_argument("--task", help="작업 id(없으면 현재 작업)")
    a.set_defaults(func=_approval)
    f = dsub.add_parser("facts", help="DB 기계 사실(호환성 수준 등) 보기·다시 읽기")
    f.add_argument("--refresh", action="store_true")
    f.set_defaults(func=_facts)
    p.set_defaults(func=lambda args: (p.print_help(), 0)[1])


def _root():
    from vmc_harness.core import paths
    root = paths.find_project_root()
    if not root:
        print("하네스가 설치되지 않은 프로젝트", file=sys.stderr)
    return root


def _task_id(root, given):
    if given:
        return given
    if os.environ.get("VMC_HARNESS_TASK_ID"):
        return os.environ["VMC_HARNESS_TASK_ID"]
    try:
        from vmc_harness.task import state
        resolve = getattr(state, "resolve_session_id", None)
        sid = resolve(root) if resolve else None
        cur = state.current(root, sid) if sid else None
        if cur:
            return cur.get("task_id")
    except (ImportError, AttributeError):
        pass
    return None


def _print_table(res):
    cols = res["columns"]
    print("\t".join(str(c) for c in cols))
    for r in res["rows"]:
        print("\t".join("NULL" if v is None else str(v).replace("\t", " ").replace("\n", " ") for v in r))
    tail = "%d행" % len(res["rows"])
    if res["truncated"]:
        tail += " (상한 %d행에서 잘림)" % res["max_rows"]
    print("-- " + tail)


def _query(args):
    from vmc_harness.db import client, drivers
    root = _root()
    if not root:
        return 2
    try:
        res = client.query(root, args.sql)
    except client.DbRefused as e:
        print("거부: %s" % e, file=sys.stderr)
        return 2
    except client.NotRegistered as e:
        print(str(e), file=sys.stderr)
        return 3
    except drivers.DbTimeout as e:
        print(str(e), file=sys.stderr)
        return 4
    except (drivers.DbError, drivers.DriverMissing) as e:
        print("DB 오류: %s" % e, file=sys.stderr)
        return 5
    if args.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        _print_table(res)
    return 0


def _dryrun(args):
    from vmc_harness.db import client, drivers
    root = _root()
    if not root:
        return 2
    task_id = _task_id(root, args.task)
    try:
        res = client.dryrun(root, args.path, task_id=task_id)
    except client.DbRefused as e:
        print("거부: %s" % e, file=sys.stderr)
        return 2
    except client.NotRegistered as e:
        print(str(e), file=sys.stderr)
        return 3
    except (drivers.DbError, drivers.DriverMissing) as e:
        print("DB 오류: %s" % e, file=sys.stderr)
        return 5
    if args.json:
        print(json.dumps(res, ensure_ascii=False, default=str))
        return 0 if not res["error"] else 1
    print("드라이런: %s" % res["file"])
    print("  영향 행 수: %d (배치 실행 %d회)" % (res["affected_rows"], res["statements"]))
    for b in res["per_batch"]:
        print("    배치 %d (%d행~): %d행" % (b["batch"], b["line"], b["affected_rows"]))
    print("  롤백: %s" % ("완료" if res["rolled_back"] else "확인 안 됨"))
    print("  롤백 SQL: %s" % (res["rollback_sql"] or res["rollback_note"]))
    if res.get("compatibility_level") is not None:
        print("  DB 호환성 수준: %s" % res["compatibility_level"])
    if res["ignored_use"]:
        print("  무시한 USE 줄: %s" % ", ".join(res["ignored_use"]))
    for w in res.get("warnings") or []:
        print("  주의: %s" % w)
    if res["error"]:
        print("  오류: %s" % res["error"])
    if not task_id:
        print("  (작업 id 없음 — 증거로 남기지 않음)")
    return 0 if not res["error"] else 1


def _approval(args):
    from vmc_harness.core.ledger import MetaGateError
    from vmc_harness.db import client
    root = _root()
    if not root:
        return 2
    task_id = _task_id(root, args.task)
    try:
        rec = client.prepare_apply_approval(root, task_id, args.path)
    except MetaGateError as e:
        print("거부: %s" % e, file=sys.stderr)
        return 2
    print(rec["approval_id"])
    return 0


def _facts(args):
    from vmc_harness.db import facts
    root = _root()
    if not root:
        return 2
    data = facts.refresh(root) if args.refresh else facts.load(root)
    if data is None:
        print("다시 읽기 실패(접속 정보 미등록 또는 연결 실패)", file=sys.stderr)
        return 1
    for f in data.get("facts", []):
        print("%s = %s    # %s" % (f["key"], f["value"], f["command"]))
    return 0
