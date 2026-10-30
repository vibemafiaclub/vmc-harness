"""`vmc-harness activate [--offline] <키> [--bundle <zip>]` — 라이선스 확인 → 커널 받기(spec 10).

설치된 껍데기(bin/vmc_harness_shell.py)는 이 명령을 커널 없이 직접 처리한다. 여기는 엔진에서 부를 때의 같은 입구.
"""


def register(sub):
    p = sub.add_parser("activate", help="라이선스 키로 활성화하고 커널을 받는다")
    p.add_argument("key", nargs="?", help="라이선스 키(vmh_…). 생략하면 터미널에서 입력")
    p.add_argument("--offline", action="store_true", help="서버 없이 오프라인 번들로")
    p.add_argument("--bundle", help="오프라인 번들 zip 경로")
    p.set_defaults(func=_run)


def _run(args):
    from vmc_harness.dist import server, settings, shell
    argv = ([args.key] if args.key else []) + (["--offline"] if args.offline else []) + (["--bundle", args.bundle] if args.bundle else [])
    if args.offline:
        return shell.activate_main(argv, keep=settings.get("update.keep_versions"))
    return shell.activate_main(argv, base=server.base_url(),
                               check_timeout=settings.get("server.call_timeout_s"),
                               download_timeout=settings.get("update.download_timeout_s"),
                               keep=settings.get("update.keep_versions"))
