"""낡은 문서 검출 (spec 04 §의도와 다른 지침 막기 — 실패 5).

기계 검출기: 문서(추적 중인 *.md)가 가리키는 경로·심볼이 지금 코드에 없으면 "낡았을 수 있음".
- 문서는 지우지도 고치지도 않는다(고객 AGENTS.md 포함). 에이전트 참고 목록에서만 뺀다 → `.harness/cache/stale-docs.json`
  (참고 목록을 만드는 쪽은 `stale_paths(root)`/`is_stale(root, path)`로 거른다)
- 개발자가 그 문서를 열거나 그 영역(문서가 있는 폴더)을 작업할 때 한 줄 알림(notices, 문서당 한 번)
"""

import re
from pathlib import Path

from vmc_harness.core import config, notices, paths
from vmc_harness.core.clock import now_iso
from vmc_harness.core.gitutil import git, head
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.project import local_dir

CODE_EXT = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".java", ".kt", ".cs", ".go", ".rs", ".rb",
            ".php", ".sql", ".vue", ".svelte", ".c", ".h", ".cpp", ".hpp", ".swift", ".m", ".scala", ".sh", ".ps1"}
PATH_EXT = CODE_EXT | {".md", ".json", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".txt", ".html", ".css", ".scss",
                       ".xml", ".env", ".lock", ".csv"}
_INLINE = re.compile(r"`([^`\n]{2,200})`")
_LINK = re.compile(r"\]\(([^)\s]+)\)")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SYMBOL = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*(?:\(\))?$")
MAX_SOURCE_BYTES = 1 << 20


def cache_file(root):
    return paths.cache_dir(root) / "stale-docs.json"


def _tracked(root):
    rc, out, _ = git(root, "ls-files", "-z")
    if rc != 0:
        return []
    return [p for p in out.split("\0") if p]


def _is_path_ref(tok):
    if " " in tok or "://" in tok or tok.startswith(("~", "/", "$", "-", "@")) or any(c in tok for c in "*?<>{}|="):
        return False
    if re.match(r"^[A-Za-z]:[\\/]", tok):
        return False
    t = tok.split("#")[0]
    t = re.sub(r":\d+(-\d+)?$", "", t)
    ext = Path(t).suffix.lower()
    return ("/" in t and len(t) > 2) or ext in PATH_EXT


def _clean_path(tok):
    t = tok.split("#")[0]
    t = re.sub(r":\d+(-\d+)?$", "", t).replace("\\", "/")
    while t.startswith("./"):
        t = t[2:]
    return t.rstrip("/")


def _is_symbol_ref(tok):
    if not _SYMBOL.match(tok):
        return False
    core = tok[:-2] if tok.endswith("()") else tok
    last = core.split(".")[-1]
    if len(last) < 4:
        return False
    camel = re.search(r"[a-z][A-Z]", last) is not None
    return tok.endswith("()") or "_" in last.strip("_") or camel


def refs_of(text):
    paths_, symbols = set(), set()
    for tok in _INLINE.findall(text):
        tok = tok.strip()
        if _is_path_ref(tok):
            paths_.add(_clean_path(tok))
        elif _is_symbol_ref(tok):
            symbols.add(tok)
    for tok in _LINK.findall(text):
        if tok.startswith(("http:", "https:", "mailto:", "#")):
            continue
        if _is_path_ref(tok):
            paths_.add(_clean_path(tok))
    return paths_, symbols


def _path_exists(root, doc, ref, tracked_set, tracked_dirs):
    if not ref:
        return True
    cands = [ref, str((Path(doc).parent / ref)).replace("\\", "/")]
    for c in cands:
        c = c.replace("/./", "/")
        if c in tracked_set or c in tracked_dirs or (Path(root) / c).exists():
            return True
    suffix = "/" + ref
    return any(t.endswith(suffix) for t in tracked_set) or any(d.endswith(suffix) for d in tracked_dirs)


def _identifiers(root, tracked):
    idents = set()
    for rel in tracked:
        if Path(rel).suffix.lower() not in CODE_EXT:
            continue
        p = Path(root) / rel
        try:
            if p.stat().st_size > MAX_SOURCE_BYTES:
                continue
            idents.update(_IDENT.findall(p.read_text(encoding="utf-8", errors="ignore")))
        except OSError:
            continue
    return idents


def _area(doc):
    """문서가 있는 폴더. 루트 문서는 영역 없음(그 문서를 열 때만 알린다)."""
    parent = str(Path(doc).parent).replace("\\", "/")
    return None if parent in (".", "") else parent


def scan(root):
    tracked = _tracked(root)
    tracked_set = set(tracked)
    tracked_dirs = set()
    for t in tracked:
        parts = t.split("/")
        for i in range(1, len(parts)):
            tracked_dirs.add("/".join(parts[:i]))
    docs = [t for t in tracked if t.lower().endswith(".md") and not t.startswith(".harness/")]
    docs = docs[:config.get("stale_docs.max_docs", root)]
    idents = None
    min_missing = config.get("stale_docs.min_missing", root)
    out = {}
    for d in docs:
        try:
            text = (Path(root) / d).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        prefs, srefs = refs_of(text)
        if not prefs and not srefs:
            continue
        missing = [r for r in sorted(prefs) if not _path_exists(root, d, r, tracked_set, tracked_dirs)]
        if srefs:
            if idents is None:
                idents = _identifiers(root, tracked)
            for s in sorted(srefs):
                core = s[:-2] if s.endswith("()") else s
                if core.split(".")[-1] not in idents:
                    missing.append(s)
        if len(missing) >= min_missing:
            out[d] = {"missing": missing[:10], "missing_count": len(missing), "refs": len(prefs) + len(srefs),
                      "area": _area(d)}
    data = {"generated_at": now_iso(), "head": head(root), "docs": out}
    write_json(cache_file(root), data)
    return data


def load(root):
    return read_json(cache_file(root), {}) or {}


def stale_paths(root):
    return set((load(root).get("docs") or {}).keys())


def is_stale(root, path):
    return _clean_path(path) in stale_paths(root)


def _noticed_file(root):
    return local_dir(root) / "stale-noticed.json"


def notice_for(root, touched_paths):
    """개발자가 낡은 문서를 열거나 그 영역을 작업할 때 한 줄 알림(문서당 한 번). 만든 알림 목록."""
    docs = load(root).get("docs") or {}
    if not docs or not touched_paths:
        return []
    seen = read_json(_noticed_file(root), {}) or {}
    made = []
    for tp in touched_paths:
        tp = _clean_path(tp)
        for d, info in docs.items():
            if d in seen:
                continue
            area = info.get("area") or ""
            if tp == d or (area and (tp == area or tp.startswith(area + "/"))):
                n = notices.add(root, "stale_doc", "flow",
                                "%s 문서가 가리키는 것 %d개가 지금 코드에 없어요. 낡았을 수 있어 에이전트 참고에서 뺐어요."
                                % (d, info.get("missing_count", 0)), area=area or None, refs={"doc": d})
                seen[d] = now_iso()
                if n:
                    made.append(n)
    if made:
        write_json(_noticed_file(root), seen)
    return made
