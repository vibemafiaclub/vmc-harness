"""`vmc-harness map` — 구조 지도(.harness/cache/map.json·map.md) 다시 만들기·조회 (spec 08)."""

import json
import sys

from vmc_harness.core import paths


def register(sub):
    p = sub.add_parser("map", help="구조 지도 다시 만들기(모듈·공개 표면·의존·책임 한 줄)")
    p.add_argument("--no-llm", action="store_true", help="책임 한 줄(LLM) 없이")
    p.add_argument("--module-of", metavar="PATH", help="그 파일이 속한 모듈 id만 출력")
    p.add_argument("--diff", metavar="OLD_MAP_JSON", help="이전 map.json과 비교")
    p.add_argument("--bundles", action="store_true", help="Harness-Task 묶음 추적(기능별 새 모듈·의존)")
    p.add_argument("--since", help="--bundles 기간(git --since 형식)")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=run)


def run(args):
    root = paths.find_project_root()
    if root is None:
        print("하네스가 설치된 프로젝트가 아님(.harness/config.json 없음)", file=sys.stderr)
        return 2
    from vmc_harness.bootstrap import structmap
    from vmc_harness.core.jsonio import read_json
    if args.module_of:
        m = structmap.module_of(root, args.module_of)
        print(m or "")
        return 0 if m else 1
    if args.bundles:
        res = structmap.task_bundles(root, since=args.since)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            for b in res:
                print(b["text"])
        return 0
    old = read_json(args.diff) if args.diff else structmap.load(root)
    data = structmap.build(root, use_llm=not args.no_llm)
    if args.diff:
        print(json.dumps(structmap.diff(old, data), ensure_ascii=False, indent=2))
        return 0
    d = structmap.diff(old, data) if old else None
    print("구조 지도: 모듈 %d개 → %s" % (len(data["modules"]), structmap.md_path(root)))
    if d and not d["empty"]:
        print("바뀐 것: 모듈 +%d −%d · 공개 표면 %d · 의존 %d" % (len(d["added"]), len(d["removed"]),
                                                         len(d["surface_changed"]), len(d["deps_changed"])))
    return 0
