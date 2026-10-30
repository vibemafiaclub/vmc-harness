"""진입점. commands/ 아래 모듈을 자동으로 찾아 하위 명령을 등록한다."""

import argparse
import importlib
import pkgutil
import sys
import warnings

from vmc_harness import __version__, commands


# 매 도구 호출마다 새 프로세스로 뜨는 명령 — 그 명령 모듈만 import한다(나머지 명령 모듈 수십 개를 읽지 않는다. 고객 Windows 훅 기동 비용 E2)
FAST_COMMANDS = ("hook",)


def build_parser(only=None):
    parser = argparse.ArgumentParser(prog="vmc-harness", description="VMC 하네스")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command")
    names = [only] if only else [m.name for m in sorted(pkgutil.iter_modules(commands.__path__), key=lambda m: m.name)]
    for name in names:
        module = importlib.import_module("vmc_harness.commands." + name)
        register = getattr(module, "register", None)
        if register:
            register(sub)
    return parser


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        # Windows 콘솔 cp949에서 한글 출력이 깨지지 않게
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
    # 고객 코드를 ast.parse할 때 나오는 경고('\s' 이스케이프 등)는 하네스 오류가 아니다 — 화면·훅 출력에 섞지 않는다
    warnings.filterwarnings("ignore", category=SyntaxWarning)
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = build_parser(argv[0] if argv and argv[0] in FAST_COMMANDS else None)
    args = parser.parse_args(argv)
    func = getattr(args, "func", None)
    if not func:
        parser.print_help()
        return 0
    rc = func(args)
    return rc if isinstance(rc, int) else 0
