"""`vmc-harness evidence <종류> …` — 실제 동작 증거 산출물(spec 02 완료 게이트). 에이전트가 부른다.

    evidence screenshot <이미지 경로> [--target 페이지]
    evidence api_call <METHOD> <URL> [--data 본문] [--header K:V]… [--target]
    evidence test_run [--target] -- <테스트 명령…>
    evidence db_dryrun <SQL 파일>          (db 영역 dryrun 실행 → 증거)
    evidence list [--json]
작업은 --task → VMC_HARNESS_TASK_ID → 지금 세션의 착수 선언 → 없으면 세션 키(S-<session>).
"""

import json
import os
import sys

from vmc_harness.core import paths


def register(sub):
    p = sub.add_parser("evidence", help="실제 동작 증거(스크린샷·API 호출·테스트 실행 기록)")
    p.add_argument("--task", help="작업 id(없으면 지금 세션 작업)")
    p.add_argument("--session", help="세션 id")
    s = p.add_subparsers(dest="kind")
    a = s.add_parser("screenshot", help="스크린샷 파일을 증거로 남긴다")
    a.add_argument("path")
    a.add_argument("--target", help="무엇의 화면인지(페이지)")
    a = s.add_parser("api_call", aliases=["api"], help="API를 실제로 호출하고 응답을 기록")
    a.add_argument("method")
    a.add_argument("url")
    a.add_argument("--data", help="요청 본문(문자열, JSON이면 Content-Type 자동)")
    a.add_argument("--header", action="append", default=[], help="K:V")
    a.add_argument("--target")
    a = s.add_parser("test_run", aliases=["test"], help="테스트 명령 실행 기록(기준선 대비 새 실패)")
    a.add_argument("--target")
    a.add_argument("argv", nargs="+", help="-- 뒤에 테스트 명령")
    a = s.add_parser("db_dryrun", help="패치 SQL 드라이런(개발/복제 DB, 롤백) → 증거")
    a.add_argument("sql_path")
    s.add_parser("qa_scenario", help="QA 시나리오 증거는 QA 도구가 PASS 기록 때 남긴다")
    a = s.add_parser("list", help="이 작업의 증거 마커")
    a.add_argument("--json", action="store_true")
    p.set_defaults(func=run)


def resolve_task(root, explicit=None, session=None):
    if explicit:
        return explicit
    if os.environ.get("VMC_HARNESS_TASK_ID"):
        return os.environ["VMC_HARNESS_TASK_ID"]
    from vmc_harness.gates import state
    sid = session or state.env_session_id()
    try:
        from vmc_harness.task import state as tstate
        sid = tstate.resolve_session_id(str(root), session)
        t = tstate.current(str(root), sid)
        if t and t.get("task_id"):
            return t["task_id"]
    except Exception:
        pass
    return state.task_key(root, sid)


def _print(m):
    s = m.get("summary") or {}
    extra = (" " + json.dumps(s, ensure_ascii=False)) if s else ""
    if m.get("duplicate"):
        print("같은 내용의 증거가 이미 있음(다시 기록 안 함): %s %s (%s)" % (m["kind"], m.get("target") or "", m["task_id"]))
        return
    print("증거 기록: %s %s (%s)%s" % (m["kind"], m.get("target") or "", m["task_id"], extra))


def run(args):
    root = paths.find_project_root()
    if root is None:
        print("하네스가 설치된 프로젝트가 아님(.harness/config.json 없음)", file=sys.stderr)
        return 2
    from vmc_harness.gates import evidence as ev
    kind = args.kind
    if not kind:
        print("종류: screenshot | api_call | test_run | db_dryrun | list", file=sys.stderr)
        return 2
    tid = resolve_task(root, args.task, args.session)
    try:
        if kind == "screenshot":
            _print(ev.screenshot(root, tid, args.path, args.target or os.path.basename(args.path)))
        elif kind in ("api_call", "api"):
            headers = {}
            for h in args.header:
                k, _, v = h.partition(":")
                headers[k.strip()] = v.strip()
            m = ev.api_call(root, tid, args.method, args.url, target=args.target, body=args.data, headers=headers)
            _print(m)
        elif kind in ("test_run", "test"):
            argv = [a for a in args.argv if a != "--"]
            m = ev.test_run(root, tid, argv, target=args.target)
            _print(m)
            if not ev.marker_ok(m):
                data = ev.load_data(root, tid, m) or {}
                print("기준선 대비 새 실패: %s" % (", ".join(data.get("new_failures") or []) or "판독 불가(종료 코드 %s)" % data.get("rc")))
                return 1
        elif kind == "db_dryrun":
            from vmc_harness.db import client
            res = client.dryrun(str(root), args.sql_path, task_id=tid)
            print(json.dumps({k: res.get(k) for k in ("file", "affected_rows", "statements", "error", "rollback_sql")},
                             ensure_ascii=False))
            if not res.get("evidence"):
                return 1
        elif kind == "qa_scenario":
            print("QA 시나리오 증거는 `vmc-harness qa` 결과 기록(PASS) 때 남는다", file=sys.stderr)
            return 2
        elif kind == "list":
            ms = ev.list_markers(root, tid)
            if args.json:
                print(json.dumps(ms, ensure_ascii=False, indent=2))
            else:
                print("작업 %s 증거 %d개" % (tid, len(ms)))
                for m in ms:
                    print("  %s %s %s%s" % (m["created_at"][:19], m["kind"], m.get("target") or "",
                                            "" if ev.marker_ok(m) else " (인정 안 됨)"))
    except ImportError as e:
        print("도구 없음: %s" % e, file=sys.stderr)
        return 2
    except (OSError, ValueError) as e:
        print("증거 못 남김: %s" % e, file=sys.stderr)
        return 1
    except Exception as e:
        print("증거 못 남김: %s" % e, file=sys.stderr)
        return 1
    return 0
