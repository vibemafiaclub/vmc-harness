"""세션 파서 공용 — 줄 읽기, 시각, 도구 입력에서 파일 경로 뽑기."""

import json
import re
from datetime import datetime, timezone

from vmc_harness.core.clock import parse_iso

_PATCH_FILE = re.compile(r"^\s*\*\*\* (?:Add|Update|Delete) File: ([^\n\"'`]+)$", re.M)
_PATCH_MOVE = re.compile(r"^\s*\*\*\* Move to: ([^\n\"'`]+)$", re.M)
PATH_KEYS = ("file_path", "path", "notebook_path", "filePath")


def read_lines(path):
    out = []
    with open(str(path), "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue  # 깨진 줄 하나 때문에 세션을 버리지 않는다
            if isinstance(d, dict):
                out.append(d)
    return out


def ts(value):
    """여러 시각 표기 → aware datetime 또는 None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e12:
            v /= 1000.0
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    d = parse_iso(str(value))
    if d is not None and d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def iso(d):
    return d.isoformat() if d else None


def paths_from_input(inp):
    """도구 입력에서 쓰거나 읽는 파일 경로들(apply_patch 본문 포함)."""
    paths = []
    if isinstance(inp, dict):
        for k in PATH_KEYS:
            v = inp.get(k)
            if isinstance(v, str) and v:
                paths.append(v)
        for k in ("input", "patch", "arguments"):
            v = inp.get(k)
            if isinstance(v, str) and "*** " in v and "Begin Patch" in v:
                paths.extend(paths_from_patch(v))
        cmd = inp.get("command")
        if isinstance(cmd, list):
            cmd = " ".join(str(x) for x in cmd)
        if isinstance(cmd, str) and "*** Begin Patch" in cmd:
            paths.extend(paths_from_patch(cmd))
    elif isinstance(inp, str) and "*** " in inp:
        paths.extend(paths_from_patch(inp))
    seen, out = set(), []
    for p in paths:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def paths_from_patch(text):
    text = text or ""
    if "\\n" in text:  # 다른 도구 입력 문자열 안에 이스케이프된 패치(Codex exec 실측)
        text = text.replace("\\n", "\n")
    return [m.strip() for m in _PATCH_FILE.findall(text or "")] + [m.strip() for m in _PATCH_MOVE.findall(text or "")]


def span_sec(turns):
    """[(start_dt, end_dt)] → 겹치지 않게 합친 초."""
    total = 0.0
    last_end = None
    for s, e in sorted(t for t in turns if t[0] and t[1]):
        if last_end and s < last_end:
            s = last_end
        if e > s:
            total += (e - s).total_seconds()
            last_end = e if not last_end or e > last_end else last_end
    return total
