"""출고 정보 — 릴리스 때 scripts/release.py가 넣는 release_info.json(spec 11 §출고 기준).

개발 체크아웃에는 파일이 없다 → 빈 값. 고객에게 '시험 기능(고객 환경 미확인)'을 숨기지 않고 보이기 위해 있다.
"""

import json
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "release_info.json"


def unverified(path=None):
    """[{id, desc}] — 이번 커널에서 고객 환경 확인을 마치지 못한 기능."""
    try:
        data = json.loads(Path(path or PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [i for i in data.get("unverified") or [] if isinstance(i, dict) and i.get("desc")]


def notice_lines(path=None):
    items = unverified(path)
    if not items:
        return []
    return ["시험 기능(아직 고객 환경에서 끝까지 확인하지 못함):"] + ["- " + i["desc"] for i in items]
