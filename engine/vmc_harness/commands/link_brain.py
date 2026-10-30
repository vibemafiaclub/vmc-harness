"""`vmc-harness link-brain <url>` — 브레인을 팀 원격에 연결(선택). `--sync`는 세션 시작 분리 실행용."""

import json
import sys


def register(sub):
    p = sub.add_parser("link-brain", help="브레인(판단 카드 git 레포)을 팀 원격에 연결")
    p.add_argument("url", nargs="?")
    p.add_argument("--sync", action="store_true", help="원격과 동기화(pull --ff-only, 앞서면 push)")
    p.set_defaults(func=run)


def run(args):
    from vmc_harness.brain import sync
    if args.sync:
        res = sync.pull()
        print(json.dumps(res, ensure_ascii=False))
        return 0
    if not args.url:
        print("원격 주소 없음", file=sys.stderr)
        return 2
    res = sync.link(args.url)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 1 if res.get("error") else 0
