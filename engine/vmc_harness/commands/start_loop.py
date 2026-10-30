"""`vmc-harness start-loop` — 퇴근할 때(선택). 안 쳐도 start_hour 이후 idle_hours 무지시면 스케줄러가 돌린다.
스케줄러 등록이 막힌 PC(미검증 12 #8)에서는 이 명령이 루프를 돌리는 유일한 길이다."""

import os
import sys

from vmc_harness.core import config, paths


def register(sub):
    p = sub.add_parser("start-loop", help="클렌징 루프 지금 시작(퇴근)")
    p.add_argument("--foreground", action="store_true", help="백그라운드로 띄우지 않고 이 터미널에서 실행")
    p.set_defaults(func=cmd_start_loop)


def cmd_start_loop(args):
    root = paths.find_project_root()
    if not root:
        print("하네스가 설치되지 않은 프로젝트", file=sys.stderr)
        return 2
    if not config.get("loop.enabled", root):
        print("이 프로젝트는 정리 루프가 꺼져 있어요(프로파일 %s)." % config.project_profile(root))
        return 0
    from vmc_harness.loop import activity, scheduler
    if activity.is_running(root):
        print("이미 돌고 있어요.")
        return 0
    activity.request_start(root)
    stop = config.get("loop.stop_hour", root)
    if args.foreground:
        from vmc_harness.loop import tick
        r = tick.run(str(root), "start-loop")
        print("끝: %s" % r.get("reason"))
        return 0
    from vmc_harness.core.proc import spawn_detached
    cmd = scheduler.tick_command(root)
    cmd = cmd[:cmd.index("loop")] + ["loop", "run", "--root", str(root)]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(scheduler.engine_dir()) + os.pathsep + env.get("PYTHONPATH", "")
    spawn_detached(cmd, cwd=str(root), env=env)
    print("정리 루프를 시작했어요. 다시 지시하면 바로 멈추고, %02d:00에는 무조건 멈춰요. 반영은 내일 첫 세션 때예요." % stop)
    return 0
