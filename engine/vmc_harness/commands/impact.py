"""`vmc-harness impact <파일|파일:심볼|심볼>` — 호출처 역추적 → 영향 모듈·페이지·API (spec 02)."""

import json
import sys

from vmc_harness.core import paths


def register(sub):
    p = sub.add_parser("impact", help="바뀐 파일·함수의 호출처와 영향 페이지·API")
    p.add_argument("target", nargs="+", help="파일 경로, 파일:심볼, 또는 심볼 이름")
    p.add_argument("--depth", type=int, help="역추적 깊이")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=run)


def run(args):
    root = paths.find_project_root()
    if root is None:
        print("하네스가 설치된 프로젝트가 아님(.harness/config.json 없음)", file=sys.stderr)
        return 2
    from vmc_harness.gates import impact
    files = impact.source_files(root)
    results = [impact.impact(root, t, depth=args.depth, files=files) for t in args.target]
    if args.json:
        print(json.dumps(results if len(results) > 1 else results[0], ensure_ascii=False, indent=2))
    else:
        print("\n\n".join(impact.render_text(r) for r in results))
    return 0
