"""경로 glob (`**` 지원)과 경로 정규화. 저장 경로 구분자는 `/`(spec 09)."""

import os
import re
from pathlib import Path

_cache = {}


def to_regex(glob):
    if glob in _cache:
        return _cache[glob]
    i, out = 0, ["^"]
    g = glob.replace("\\", "/")
    while i < len(g):
        c = g[i]
        if g.startswith("**/", i):
            out.append("(?:.*/)?")
            i += len("**/")
            continue
        if g.startswith("**", i):
            out.append(".*")
            i += len("**")
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    out.append("$")
    rx = re.compile("".join(out), re.I if os.name == "nt" else 0)
    _cache[glob] = rx
    return rx


def match(rel, glob):
    rel = norm(rel)
    g = norm(glob)
    if g.endswith("/"):
        g += "**"
    if to_regex(g).match(rel):
        return True
    # 디렉터리 glob("auth/")·접두 경로("audit")도 그 아래 전부로
    if "*" not in g and "?" not in g and rel.startswith(g.rstrip("/") + "/"):
        return True
    return False


def match_any(rel, globs):
    for g in globs or []:
        if match(rel, g):
            return g
    return None


def norm(p):
    p = str(p).replace("\\", "/")
    while p.startswith("./"):
        p = p[len("./"):]
    return p


def rel_to(root, path, cwd=None):
    """절대·상대 경로 → 프로젝트 루트 상대(`/`). 루트 밖이면 None."""
    if path is None:
        return None
    p = Path(os.path.expanduser(str(path)))
    if not p.is_absolute():
        p = Path(cwd or root) / p
    try:
        rp = _resolve(p)
        rr = _resolve(Path(root))
        return norm(rp.relative_to(rr).as_posix())
    except ValueError:
        return None


def abspath(path, cwd):
    p = Path(os.path.expanduser(str(path)))
    if not p.is_absolute():
        p = Path(cwd) / p
    return _resolve(p)


def is_under(path, base):
    try:
        _resolve(Path(path)).relative_to(_resolve(Path(base)))
        return True
    except ValueError:
        return False


def _resolve(p):
    # 존재하지 않는 파일도 상위의 심볼릭 링크(/tmp → /private/tmp)를 풀어 비교한다
    p = Path(os.path.abspath(str(p)))
    parts = []
    cur = p
    while not cur.exists() and cur != cur.parent:
        parts.append(cur.name)
        cur = cur.parent
    base = cur.resolve()
    for name in reversed(parts):
        base = base / name
    return base
