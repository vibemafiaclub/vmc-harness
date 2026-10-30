"""bootstrap 영역 공용: 소스 파일 순회, 상대 경로, 다른 영역 함수 선택 호출."""

import fnmatch
import importlib
import os
import re
from pathlib import Path

IGNORE_DIRS = {
    ".git", ".harness", ".hg", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
    ".next", ".nuxt", "dist", "build", "out", "coverage", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".idea", ".vscode", ".turbo", ".cache", "site-packages", ".claude",
    ".codex", "target", "bin", "obj", ".gradle", "vendor", ".eggs",
}
PY_EXT = {".py"}
JS_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
SOURCE_EXT = PY_EXT | JS_EXT | {".cs", ".java", ".go", ".kt", ".rb", ".php", ".rs", ".sql", ".vue", ".svelte"}


def rel(root, p):
    """프로젝트 루트 기준 상대 경로(구분자 /)."""
    try:
        return Path(p).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return Path(p).as_posix()


def git_files(base):
    """git이 아는 파일(추적 + .gitignore에 안 걸린 미추적) — base 기준 상대 경로(구분자 /) 정렬 목록.
    git 작업 트리가 아니거나 git이 없으면 None(호출자가 폴더 순회로 폴백). -z라 한글 경로가 이스케이프되지 않는다."""
    from vmc_harness.core.gitutil import git
    rc, out, _ = git(base, "-c", "core.quotepath=off", "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                     timeout=120)
    if rc != 0:
        return None
    return sorted(set(x for x in out.split("\0") if x))


def _ignored(rel_path):
    parts = rel_path.split("/")
    return any(d in IGNORE_DIRS or d.endswith(".egg-info") for d in parts[:-1])


def walk_files(base, exts=None, limit=None):
    """base 아래 파일(무시 폴더 제외). exts가 있으면 그 확장자만. (경로 목록, 잘렸는지)
    git 작업 트리면 `git ls-files`(추적 + 무시 안 된 미추적) — 고객 .gitignore가 빼 둔 빌드 산출물·사본을 지도·후보에 넣지 않는다(D6).
    git이 아니면 폴더 순회."""
    out = []
    base = Path(base)
    listed = git_files(base) if base.is_dir() else None
    if listed is not None:
        for r in listed:
            if _ignored(r) or (exts and Path(r).suffix.lower() not in exts):
                continue
            f = base / r
            if not f.is_file():  # 지운 채 커밋 안 한 파일·서브모듈
                continue
            out.append(f)
            if limit and len(out) >= limit:
                return out, True
        return out, False
    for dirpath, dirnames, filenames in os.walk(str(base)):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS and not d.endswith(".egg-info"))
        for fn in sorted(filenames):
            if exts and Path(fn).suffix.lower() not in exts:
                continue
            out.append(Path(dirpath) / fn)
            if limit and len(out) >= limit:
                return out, True
    return out, False


def read_text(p, limit=None):
    try:
        with open(str(p), "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit) if limit else f.read()
    except OSError:
        return ""


def glob_match(path, pattern):
    """'**'를 지원하는 경로 glob (구분자 /). 'a/**'는 a 아래 전부."""
    path = path.replace("\\", "/")
    pattern = pattern.replace("\\", "/")
    rx = _glob_rx(pattern)
    return bool(rx.match(path))


_GLOB_CACHE = {}


def _glob_rx(pattern):
    if pattern in _GLOB_CACHE:
        return _GLOB_CACHE[pattern]
    i, out = 0, ""
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
            continue
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
            continue
        if c == "*":
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        else:
            out += re.escape(c)
        i += 1
    rx = re.compile("^" + out + "$", re.I if os.name == "nt" else 0)
    _GLOB_CACHE[pattern] = rx
    return rx


def any_glob(path, patterns):
    return any(glob_match(path, g) for g in patterns or [])


def optional_call(candidates, *args, **kwargs):
    """다른 영역 함수가 있으면 부른다(docs/interfaces.md — 없으면 건너뜀).

    candidates: [(모듈, 함수)] 순서대로 처음 있는 것.
    돌려주는 값: ("ok", 결과) | ("skipped", None) | ("error", 메시지)
    """
    for mod_name, fn_name in candidates:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        fn = getattr(mod, fn_name, None)
        if fn is None:
            continue
        try:
            return "ok", fn(*args, **kwargs)
        except Exception as e:  # 편의 경로 — 남의 영역 오류가 설치를 멈추지 않게(P8)
            return "error", "%s.%s: %s" % (mod_name, fn_name, e)
    return "skipped", None


def has_module(mod_name):
    try:
        importlib.import_module(mod_name)
        return True
    except ImportError:
        return False


_HOME_RX = [
    re.compile(r"(?i)([A-Z]:\\Users\\)[^\\/\s\"']+"),
    re.compile(r"(/Users/|/home/)[^/\s\"']+"),
]


def scrub_user(text):
    """사람 이름이 드러나는 홈 경로를 가린다(리포트 실명 지목 없음)."""
    if not text:
        return text
    for rx in _HOME_RX:
        text = rx.sub(lambda m: m.group(1) + "…", text)
    return text


def fn_match_any(name, patterns):
    return any(fnmatch.fnmatch(name, p) for p in patterns)
