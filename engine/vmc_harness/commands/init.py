"""`vmc-harness init` — 프로젝트당 1회 설치·부트스트랩·진단 리포트 (spec 07). 이미 있으면 갱신."""

import sys
from pathlib import Path


def register(sub):
    p = sub.add_parser("init", help="이 프로젝트에 하네스 설치(훅·MCP·규칙 블록) + 부트스트랩 + 진단 리포트")
    p.add_argument("path", nargs="?", default=".", help="프로젝트 루트(프로젝트 부모 폴더, 기본 지금 폴더)")
    p.add_argument("--profile", choices=("standard", "gxp", "prototype", "solo"), help="기본 standard")
    p.add_argument("--source-roots", nargs="*", help="소스 루트(기본 자동 탐지)")
    p.add_argument("--dry-system", action="store_true",
                   help="이 PC 사용자 설정(~/.claude/settings.json·OS 스케줄러·~/.codex)을 바꾸지 않는다")
    p.add_argument("--sessions-dir", help="세션 기록 폴더(다른 PC에서 내보낸 묶음). 기본: 이 PC 세션")
    p.add_argument("--analysis-dir", help="사람 프롬프트 정제본·전수 태깅 폴더(prompts.jsonl·tag_*.md·stats.json)")
    p.add_argument("--no-tests", action="store_true", help="테스트 기준선 실행 생략(수집만)")
    p.add_argument("--no-llm", action="store_true", help="LLM 호출 없이(책임 한 줄·학습 후보·진단 생략)")
    p.add_argument("--no-learning", action="store_true", help="학습 후보 추출 생략")
    p.add_argument("--no-diagnose", action="store_true", help="시니어식 진단 생략(리포트는 만든다)")
    p.add_argument("--no-open", action="store_true", help="리포트를 브라우저로 열지 않는다")
    p.add_argument("--report-out", help="리포트 HTML·PDF 사본을 둘 폴더")
    p.add_argument("--print-fact", metavar="RULE_ID", help=argparse_suppress())
    p.set_defaults(func=run)


def argparse_suppress():
    import argparse
    return argparse.SUPPRESS


def run(args):
    root = Path(args.path).resolve()
    if args.print_fact:
        from vmc_harness.bootstrap import facts
        from vmc_harness.core import paths
        r = paths.find_project_root(root) or root
        v = facts.print_fact(r, args.print_fact)
        if v is None:
            return 1
        print(v)
        return 0
    if not root.is_dir():
        print("폴더 없음: %s" % root, file=sys.stderr)
        return 2
    from vmc_harness.bootstrap import init
    init.run(root, profile=args.profile, dry_system=args.dry_system, sessions_dir=args.sessions_dir,
             analysis_dir=args.analysis_dir, run_tests=not args.no_tests, use_llm=not args.no_llm,
             do_diagnose=not args.no_diagnose, learning=not args.no_learning, open_report=not args.no_open,
             report_out=args.report_out, source_roots=args.source_roots)
    return 0
