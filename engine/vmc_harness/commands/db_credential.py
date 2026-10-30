"""`vmc-harness db-credential` — 개발 DB 접속 정보를 사람이 터미널에서 직접 입력(spec 08·10). 비밀번호는 키체인."""

import sys


def register(sub):
    p = sub.add_parser("db-credential", help="개발 DB 읽기 전용 계정 등록(터미널 직접 입력, 비밀번호는 키체인)")
    p.add_argument("--purpose", choices=("query", "dryrun"), default="query",
                   help="query=조회용 읽기 전용 계정(기본), dryrun=드라이런용 개발/복제 DB 계정(항상 롤백)")
    p.add_argument("--server")
    p.add_argument("--port")
    p.add_argument("--database")
    p.add_argument("--user")
    p.add_argument("--show", action="store_true", help="등록 상태 보기(비밀번호 제외)")
    p.add_argument("--delete", action="store_true", help="이 프로파일 등록 삭제(키체인 포함)")
    p.set_defaults(func=_run)


def _run(args):
    from vmc_harness.core import paths
    from vmc_harness.db import client, credential
    root = paths.find_project_root()
    if not root:
        print("하네스가 설치되지 않은 프로젝트", file=sys.stderr)
        return 2
    if args.show:
        data = client.load_db_json(root)
        if not data:
            print("등록 없음")
        for prof, info in data.items():
            print("%s: %s%s / %s / %s (드라이버 %s, 비밀번호 저장소 %s, 등록 %s)" % (
                prof, info.get("server"), ("," + info["port"]) if info.get("port") else "", info.get("database"),
                info.get("user"), info.get("driver"), info.get("secret_store"), info.get("registered_at")))
        return 0
    if args.delete:
        info = client.remove_profile(root, args.purpose)
        print("삭제함" if info else "등록 없음")
        return 0
    prefill = {k: getattr(args, k) for k in ("server", "port", "database", "user") if getattr(args, k)}
    try:
        credential.interactive(root, args.purpose, prefill)
    except credential.CredentialRefused as e:
        print("거부: %s" % e, file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\n취소함", file=sys.stderr)
        return 1
    except Exception as e:  # 연결 실패 등 — 비밀값은 메시지에 없다(드라이버 오류는 마스킹)
        from vmc_harness.core.mask import mask
        print("등록 실패: %s" % mask(str(e)), file=sys.stderr)
        return 1
    return 0
