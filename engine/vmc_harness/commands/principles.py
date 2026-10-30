"""`vmc-harness principles` — 커널 원칙(PRINCIPLES.md)을 출력한다. 에이전트는 읽기만 한다(01)."""

from pathlib import Path


def _run(args):
    p = Path(__file__).resolve().parent.parent / "PRINCIPLES.md"
    print(p.read_text(encoding="utf-8"))
    return 0


def register(sub):
    p = sub.add_parser("principles", help="커널 원칙 보기")
    p.set_defaults(func=_run)
