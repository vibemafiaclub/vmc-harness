"""영향 반경 (spec 02 — `vmc-harness impact <파일|파일:심볼|심볼>`).

바뀐 함수·파일의 호출처를 역추적해 영향 모듈·페이지·API를 돌려준다. 완료 게이트에서 무엇으로 증명할지는 모델이 고른다.
- Python: ast로 import(상대 import 포함)·심볼 참조
- TS/JS: import·require 경로 해석(상대 경로, `@/`·`~/` 별칭은 접미 일치) + 심볼 이름 grep 수준
- 심볼만 주면 단어 경계 grep
"""

import ast
import os
import re
from pathlib import Path

from vmc_harness.core import config
from vmc_harness.core.gitutil import git
from vmc_harness.gates import globs
from vmc_harness.gates.common import behavior_globs, behavior_kind

PY_EXT = (".py",)
JS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte")
_JS_IMPORT = re.compile(r"""(?:import\s[^'"]*?from\s*|import\s*\(\s*|require\s*\(\s*|export\s[^'"]*?from\s*|import\s+)['"]([^'"]+)['"]""")


def source_files(root):
    rc, out, _ = git(root, "ls-files", "--cached", "--others", "--exclude-standard")
    if rc == 0:
        files = [l.strip() for l in out.splitlines() if l.strip()]
    else:
        files = []
        for dp, dn, fn in os.walk(str(root)):
            dn[:] = [d for d in dn if not d.startswith(".") and d not in ("node_modules", "__pycache__", "dist", "build")]
            for f in fn:
                files.append(globs.norm(os.path.relpath(os.path.join(dp, f), str(root))))
    limit = int(config.get("gates.impact_max_files", root))
    maxb = int(config.get("gates.impact_max_file_bytes", root))
    out = []
    for f in files:
        if not f.endswith(PY_EXT + JS_EXT) or "node_modules/" in f:
            continue
        p = Path(root) / f
        try:
            if p.stat().st_size > maxb:
                continue
        except OSError:
            continue
        out.append(f)
        if len(out) >= limit:
            break
    return out


def _read(root, rel):
    try:
        return (Path(root) / rel).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _py_module_names(rel):
    """be/app/x.py → {'be.app.x','app.x','x'} (소스 루트를 모르니 접미 전부)."""
    parts = rel[:-len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return {".".join(parts[i:]) for i in range(len(parts)) if parts[i:]}


def _py_imports(rel, text):
    """[(모듈 dotted, [이름…])] — 상대 import는 파일 위치 기준으로 푼다."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return [], set()
    pkg = rel[:-len(".py")].split("/")[:-1]
    out, names = [], set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.append((a.name, []))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg[: len(pkg) - (node.level - 1)] if node.level > 1 else pkg
                mod = ".".join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ""
            out.append((mod, [a.name for a in node.names]))
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return out, names


def _strip_ext(rel):
    for e in JS_EXT:
        if rel.endswith(e):
            return rel[: -len(e)]
    return rel


def _js_targets(rel):
    s = _strip_ext(rel)
    out = {s}
    if s.endswith("/index"):
        out.add(s[: -len("/index")])
    return out


def _js_resolves(importer, spec, target_rel):
    tset = _js_targets(target_rel)
    if spec.startswith("."):
        base = os.path.normpath(os.path.join(os.path.dirname(importer), spec)).replace("\\", "/")
        return _strip_ext(base) in tset
    for prefix in ("@/", "~/", "src/", "#/"):
        if spec.startswith(prefix):
            tail = _strip_ext(spec[len(prefix):])
            return any(t == tail or t.endswith("/" + tail) for t in tset)
    return False


def callers_of(root, target_rel, symbol, files, cache):
    """target 파일(과 심볼)을 쓰는 파일 목록."""
    out = []
    is_py = target_rel and target_rel.endswith(".py")
    mods = _py_module_names(target_rel) if is_py else set()
    word = re.compile(r"\b%s\b" % re.escape(symbol)) if symbol else None
    for f in files:
        if f == target_rel:
            continue
        text = cache.get(f)
        if text is None:
            text = cache[f] = _read(root, f)
        if not target_rel:
            if word and word.search(text):
                out.append(f)
            continue
        if is_py and f.endswith(".py"):
            imps, names = _py_imports(f, text)
            hit = False
            for mod, imported in imps:
                if mod in mods:
                    hit = (not symbol) or symbol in imported or symbol in names
                elif any(mod + "." + n in mods for n in imported):
                    hit = (not symbol) or symbol in names
                if hit:
                    break
            if hit:
                out.append(f)
        elif not is_py and f.endswith(JS_EXT):
            specs = _JS_IMPORT.findall(text)
            if any(_js_resolves(f, s, target_rel) for s in specs):
                if not symbol or (word and word.search(text)):
                    out.append(f)
    return out


def _module_of(root, rel):
    try:
        from vmc_harness.bootstrap import structmap
        m = structmap.module_of(str(root), rel)
        if m:
            return m
    except Exception:
        pass
    return os.path.dirname(rel) or "."


def impact(root, target, depth=None, files=None):
    """{'target','symbol','callers':[{'path','depth','kind'}],'modules':[...],'pages':[...],'apis':[...],'db':[...]}"""
    depth = int(depth or config.get("gates.impact_max_depth", root))
    rel, symbol = _parse_target(root, target)
    files = files if files is not None else source_files(root)
    cache = {}
    seen = {}
    frontier = [(rel, symbol)]
    for d in range(1, depth + 1):
        nxt = []
        for t_rel, t_sym in frontier:
            for c in callers_of(root, t_rel, t_sym, files, cache):
                if c in seen or c == rel:
                    continue
                seen[c] = d
                nxt.append((c, None))
        frontier = nxt
        if not frontier:
            break
    bg = behavior_globs(root)
    callers = [{"path": p, "depth": d, "kind": behavior_kind(root, p, bg)} for p, d in sorted(seen.items(), key=lambda x: (x[1], x[0]))]
    res = {"target": rel, "symbol": symbol, "callers": callers,
           "modules": sorted({_module_of(root, c["path"]) for c in callers}),
           "pages": [c["path"] for c in callers if c["kind"] == "page"],
           "apis": [c["path"] for c in callers if c["kind"] == "api"],
           "db": [c["path"] for c in callers if c["kind"] == "db"]}
    own = behavior_kind(root, rel, bg) if rel else None
    if own:
        res[{"page": "pages", "api": "apis", "db": "db"}[own]].insert(0, rel)
    return res


def _parse_target(root, target):
    t = target.strip()
    sym = None
    if ":" in t and not re.match(r"^[A-Za-z]:[\\/]", t):
        t, sym = t.rsplit(":", 1)
    p = Path(t)
    if (Path(root) / t).exists() or p.exists():
        rel = globs.rel_to(root, t, os.getcwd() if not (Path(root) / t).exists() else root)
        return rel, sym
    # 파일이 아니면 심볼 이름
    return None, target.strip()


def render_text(res):
    lines = ["대상: %s%s" % (res["target"] or "-", (" :" + res["symbol"]) if res.get("symbol") else "")]
    lines.append("호출처 %d개, 모듈 %d개" % (len(res["callers"]), len(res["modules"])))
    for key, label in (("pages", "페이지"), ("apis", "API"), ("db", "DB")):
        if res[key]:
            lines.append("%s: %s" % (label, ", ".join(res[key])))
    if res["modules"]:
        lines.append("모듈: %s" % ", ".join(res["modules"]))
    for c in res["callers"]:
        lines.append("  %s%s (깊이 %d)" % (c["path"], " [%s]" % c["kind"] if c["kind"] else "", c["depth"]))
    return "\n".join(lines)
