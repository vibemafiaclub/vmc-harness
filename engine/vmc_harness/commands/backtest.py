"""`vmc-harness backtest` — 후보 게이트 조건을 지난 커밋·세션 도구 호출에 돌려 막았을 횟수·실행 시간 (spec 07)."""

import json
import sys

from vmc_harness.core import paths


def register(sub):
    p = sub.add_parser("backtest", help="후보 게이트 조건 백테스트(지난 커밋·세션) — 최소 N건")
    p.add_argument("--condition", help='조건 JSON 예: {"type":"path_write","globs":["SQL/**"]}')
    p.add_argument("--path", nargs="*", help="경로 glob(path_write / diff_regex·test_modify 범위)")
    p.add_argument("--outside", nargs="*", help="허용 폴더 glob — 그 밖 쓰기(write_outside)")
    p.add_argument("--ext", nargs="*", help="write_outside 대상 확장자(.sql …)")
    p.add_argument("--command-regex", help="셸 명령 정규식(command)")
    p.add_argument("--db-direct", action="store_true", help="DB 직접 접속 명령")
    p.add_argument("--test-modify", action="store_true", help="기존 테스트 수정·삭제")
    p.add_argument("--diff", help="커밋에 새로 들어간 줄 정규식(diff_regex)")
    p.add_argument("--sessions-dir", help="세션 묶음 폴더(기본: init이 만든 세션 색인)")
    p.add_argument("--analysis-dir")
    p.add_argument("--max-scenes", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=run)


def run(args):
    root = paths.find_project_root()
    if root is None:
        print("하네스가 설치된 프로젝트가 아님(.harness/config.json 없음)", file=sys.stderr)
        return 2
    from vmc_harness.bootstrap import backtest, diagnose, sessions_input
    try:
        cond = backtest.condition_from_args(args)
    except (ValueError, json.JSONDecodeError) as e:
        print("조건 오류: %s" % e, file=sys.stderr)
        return 2
    if args.sessions_dir:
        sessions, _ = sessions_input.load(root, args.sessions_dir, args.analysis_dir)
    else:
        sessions = diagnose.read_sessions_index(root)
    try:
        res = backtest.run(root, cond, sessions, max_scenes=args.max_scenes)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print("%s (커밋 %d · 세션 %d · 문제가 된 변경 %d · %s초)" % (res["note"], res["commits"], res["sessions"],
                                                          res["problem_changes"], res["elapsed_sec"]))
        for s in res["scenes"][:10]:
            print("  %s %s %s" % ((s.get("at") or "")[:10], s["ref"], s["match"][:100].replace("\n", " ")))
    return 0
