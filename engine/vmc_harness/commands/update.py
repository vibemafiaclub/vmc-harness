"""`vmc-harness update` — 서버 최신 커널을 확인해 다르면 받는다(spec 10). 세션 시작 훅이 하루 1회 백그라운드로 부른다.

  update               지금 확인·설치(자동 업데이트를 꺼 둬도 동작)
  update --auto off    이 PC의 세션 시작 자동 확인을 끈다(on이면 다시 켠다)
"""


def register(sub):
    p = sub.add_parser("update", help="하네스 커널 업데이트(보통 자동)")
    p.add_argument("--quiet", action="store_true", help="출력 없이")
    p.add_argument("--auto", choices=("on", "off"), help="이 PC의 세션 시작 자동 업데이트 켜기·끄기(업데이트는 하지 않는다)")
    p.set_defaults(func=_run)


def _run(args):
    from vmc_harness.dist import updater
    if args.auto:
        updater.set_auto(args.auto == "on")
        if not args.quiet:
            print("자동 업데이트 켜짐 — 세션 시작 때 하루 1회 확인" if args.auto == "on"
                  else "자동 업데이트 꺼짐 — 수동으로 받으려면 vmc-harness update, 다시 켜려면 vmc-harness update --auto on")
        return 0
    r = updater.run()
    if not args.quiet:
        msg = {"updated": "업데이트: %s → %s" % (r.get("from"), r.get("version")),
               "current": "최신 버전 사용 중: %s" % r.get("version"),
               "unreachable": "서버에 닿지 않아 지금 버전으로 진행",
               "unauthorized": "라이선스가 업데이트를 받을 수 없는 상태(%s) — 지금 버전은 계속 동작" % r.get("reason"),
               "no_license": "활성화 전 — vmc-harness activate <키>",
               "no_release": "서버에 릴리스 없음"}.get(r["status"], "업데이트 실패: %s" % r.get("detail"))
        print(msg)
        if r["status"] == "updated":  # 지금 도는 건 옛 버전 코드라 새 버전 폴더의 출고 정보를 읽는다
            from vmc_harness.core import release_info
            from vmc_harness.dist import shell
            eng = shell.engine_dir(r.get("version"))
            for line in release_info.notice_lines(eng / "vmc_harness" / release_info.PATH.name if eng else None):
                print(line)
    return 0 if r["status"] != "error" else 1
