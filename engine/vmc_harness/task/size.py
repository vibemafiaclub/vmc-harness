"""작업 크기 (spec 03 §작업 크기) — 하네스 자신의 비용 조절.

소 = 선언 파일 `task.small_max_files`개 이하 · P50 `task.small_max_p50_sec`초 이하. 그 외 중·대(mid).
디렉터리·모듈·glob 항목은 파일 수를 알 수 없으므로 소로 보지 않는다.
"""

from pathlib import PurePosixPath

from vmc_harness.core import config

SMALL, MID = "small", "mid"


def _is_file_entry(e):
    e = str(e or "").replace("\\", "/").strip().rstrip("/")
    return bool(e) and not any(ch in e for ch in "*?[") and bool(PurePosixPath(e).suffix)


def _is_path_like(e):
    e = str(e or "")
    return "/" in e or bool(PurePosixPath(e).suffix) or any(ch in e for ch in "*?[")


def declared_files(declare):
    """선언 범위 + 새로 만들 것 중 경로 항목. `new`의 의존성 이름(예: lodash)은 파일이 아니므로 세지 않는다."""
    entries = []
    for x in (declare.get("scope") or []) + [n for n in (declare.get("new") or []) if _is_path_like(n)]:
        x = str(x).replace("\\", "/").strip()
        if x and x not in entries:
            entries.append(x)
    return entries


def judge(declare, root=None):
    entries = declared_files(declare)
    max_files = config.get("task.small_max_files", root)
    max_p50 = config.get("task.small_max_p50_sec", root)
    if not entries or not all(_is_file_entry(e) for e in entries):
        return MID
    if len(entries) > max_files:
        return MID
    p50 = ((declare.get("predict") or {}).get("agent_sec") or {}).get("p50")
    if p50 is None or float(p50) > max_p50:
        return MID
    return SMALL
