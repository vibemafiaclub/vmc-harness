"""`vmc-harness loop` — 클렌징 루프 (spec 06). 주로 스케줄러·훅이 부른다.

  loop tick [--root R]       퇴근 판정 → 돌릴 때면 run (스케줄러가 check_interval_min마다)
  loop run [--root R]        지금 바로 실행(start-loop가 백그라운드로 띄운다)
  loop status [--json]       루프 상태·준비된 정리·최근 실행
  loop merge [--root R]      준비된 A·B 정리를 main에 반영(세션 시작 훅이 띄운다)
  loop detect <unreferenced|duplicates|giant|wander> [--json]   검출 도구(모델이 쓸 수 있다)
  loop register | unregister OS 스케줄러 작업 등록·해제(init·remove가 부른다)
"""

import json
import sys

from vmc_harness.core import paths


def register(sub):
    p = sub.add_parser("loop", help="클렌징 루프")
    s = p.add_subparsers(dest="loop_cmd")
    for name, fn in (("tick", cmd_tick), ("run", cmd_run), ("merge", cmd_merge),
                     ("register", cmd_register), ("unregister", cmd_unregister)):
        x = s.add_parser(name)
        x.add_argument("--root")
        x.set_defaults(func=fn)
    st = s.add_parser("status")
    st.add_argument("--root")
    st.add_argument("--json", action="store_true")
    st.set_defaults(func=cmd_status)
    d = s.add_parser("detect")
    d.add_argument("kind", choices=("unreferenced", "duplicates", "giant", "wander"))
    d.add_argument("--root")
    d.add_argument("--json", action="store_true")
    d.set_defaults(func=cmd_detect)
    p.set_defaults(func=cmd_status, json=False, root=None)


def _root(args):
    root = getattr(args, "root", None) or paths.find_project_root()
    if not root or not (paths.harness_dir(root) / "config.json").is_file():
        print("하네스가 설치되지 않은 프로젝트", file=sys.stderr)
        return None
    return str(root)


def cmd_tick(args):
    root = _root(args)
    if not root:
        return 2
    from vmc_harness.loop import tick
    print(json.dumps(tick.tick(root), ensure_ascii=False))
    return 0


def cmd_run(args):
    root = _root(args)
    if not root:
        return 2
    from vmc_harness.loop import tick
    print(json.dumps(tick.run(root, "start-loop"), ensure_ascii=False))
    return 0


def cmd_merge(args):
    root = _root(args)
    if not root:
        return 2
    from vmc_harness.loop import merge
    print(json.dumps(merge.morning(root), ensure_ascii=False))
    return 0


def cmd_register(args):
    root = _root(args)
    if not root:
        return 2
    from vmc_harness.loop import scheduler
    r = scheduler.register(root)
    print(json.dumps(r, ensure_ascii=False))
    return 0 if r.get("ok") else 1


def cmd_unregister(args):
    root = _root(args)
    if not root:
        return 2
    from vmc_harness.loop import scheduler
    scheduler.unregister(root)
    print("해제함")
    return 0


def cmd_status(args):
    root = _root(args)
    if not root:
        return 2
    from vmc_harness.loop import tick
    st = tick.status(root)
    if getattr(args, "json", False):
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0
    print("루프: %s%s · 예약: %s" % ("켜짐" if st["enabled"] else "꺼짐(프로파일)",
                                   " · 실행 중" if st["running"] else "",
                                   (st["scheduler"] or {}).get("method") or "없음"))
    print("대기 후보 %d · 머지 대기 %d · 반영됨 %d · 이번 밤 토큰 %d" %
          (st["open_candidates"], len(st["prepared"]), st["merged"], st["night_tokens"]))
    for a in st["prepared"]:
        print("- [%s] %s %s" % (a["grade"], a["action_id"], a["hypothesis"]))
    for r in st["last_runs"]:
        print("  %s %s %s" % (r.get("at"), r.get("event"), r.get("reason") or r.get("trigger") or ""))
    return 0


def cmd_detect(args):
    root = _root(args)
    if not root:
        return 2
    from vmc_harness.loop import detect
    res = getattr(detect, args.kind)(root)
    print(json.dumps(res, ensure_ascii=False, indent=None if args.json else 2))
    return 0
