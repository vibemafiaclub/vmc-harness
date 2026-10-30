"""구조 지도 (spec 08 §구조 지도).

- 모듈 = 소스 루트 아래 디렉터리(소스 파일이 바로 들어 있는 폴더), 파일은 모듈 내부
- 모듈 id = 프로젝트 루트(= 프로젝트 부모 폴더) 기준 상대 경로, 예 `be/framework_core/work_requests`
- 필드: 모듈 id, 공개 표면(export), 의존 모듈, 책임 한 줄
  공개 표면·의존은 AST·정규식으로 결정적으로, 책임 한 줄만 LLM(공개 표면이 바뀔 때만 다시, 실패하면 비움)
- 경로 고정: `.harness/cache/map.json`(기계용)·`map.md`(사람용)
- 묶음 추적: 커밋 trailer `Harness-Task` 단위 "기능 X 추가(날짜): 모듈 N개 생김, 의존 a→b"
"""

import ast
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

from vmc_harness.bootstrap import util
from vmc_harness.core import config, gitutil, paths
from vmc_harness.core.clock import now_iso
from vmc_harness.core.jsonio import read_json, write_json

MAP_JSON = "map.json"
MAP_MD = "map.md"
_TS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def map_path(root):
    return paths.cache_dir(root) / MAP_JSON


def md_path(root):
    return paths.cache_dir(root) / MAP_MD


def load(root):
    return read_json(map_path(root))


# ---------------------------------------------------------------- 공개 표면

def py_surface(text):
    """Python 파일의 공개 이름. __all__이 있으면 그것, 없으면 _로 시작하지 않는 최상위 def·class."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    names, explicit = [], None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "__all__" and isinstance(node.value, (ast.List, ast.Tuple)):
                    explicit = [e.value for e in node.value.elts
                                if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.name.startswith("_"):
            names.append(node.name)
    return explicit if explicit is not None else names


_TS_EXPORT = re.compile(
    r"^\s*export\s+(?:declare\s+)?(default\s+)?(?:async\s+)?(?:abstract\s+)?"
    r"(?:function\*?|class|const|let|var|interface|type|enum|namespace)\s+([A-Za-z_$][\w$]*)", re.M)
_TS_EXPORT_DEFAULT = re.compile(r"^\s*export\s+default\s+(?!function|class|async|abstract)", re.M)
_TS_EXPORT_LIST = re.compile(r"^\s*export\s+(?:type\s+)?\{([^}]*)\}", re.M)


def ts_surface(text):
    names = []
    for m in _TS_EXPORT.finditer(text):
        names.append("default" if m.group(1) and not m.group(2) else m.group(2))
    if _TS_EXPORT_DEFAULT.search(text):
        names.append("default")
    for m in _TS_EXPORT_LIST.finditer(text):
        for part in m.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            alias = part.split(" as ")[-1].strip()
            names.append(alias.replace("type ", "").strip())
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


# ---------------------------------------------------------------- 의존(import 해석)

_PY_IMPORT_RX = None


def py_imports(text):
    """[(level, 모듈 경로, [이름])]"""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.append((0, a.name, []))
        elif isinstance(node, ast.ImportFrom):
            out.append((node.level or 0, node.module or "", [a.name for a in node.names]))
    return out


_TS_IMPORT = re.compile(
    r"""(?:import\s+(?:type\s+)?(?:[^'"]*?\s+from\s+)?|export\s+(?:type\s+)?[^'"]*?\s+from\s+|require\(\s*|import\(\s*)['"]([^'"]+)['"]""")


def ts_imports(text):
    return [m.group(1) for m in _TS_IMPORT.finditer(text)]


def _strip_json_comments(text):
    out, i, q = [], 0, False
    while i < len(text):
        c = text[i]
        if q:
            out.append(c)
            if c == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                q = False
        elif c == '"':
            q = True
            out.append(c)
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = len(text) if j < 0 else j
            continue
        elif text.startswith("/*", i):
            j = text.find("*/", i)
            i = len(text) if j < 0 else j + 2
            continue
        else:
            out.append(c)
        i += 1
    s = "".join(out)
    return re.sub(r",(\s*[}\]])", r"\1", s)


def _ts_aliases(root, files):
    """tsconfig/jsconfig paths → [(접두, [대상 디렉터리 rel])]"""
    out = []
    for f in files:
        name = PurePosixPath(f).name
        if name not in ("tsconfig.json", "jsconfig.json"):
            continue
        try:
            data = json.loads(_strip_json_comments(util.read_text(Path(root) / f)))
        except ValueError:
            continue
        co = data.get("compilerOptions") or {}
        base_dir = PurePosixPath(f).parent
        base = (base_dir / (co.get("baseUrl") or ".")).as_posix()
        for key, targets in (co.get("paths") or {}).items():
            prefix = key[:-1] if key.endswith("*") else key
            tdirs = []
            for t in targets or []:
                t = t[:-1] if t.endswith("*") else t
                tdirs.append(_norm(base + "/" + t))
            out.append((prefix, tdirs))
    out.sort(key=lambda x: -len(x[0]))
    return out


def _norm(p):
    parts = []
    for seg in p.replace("\\", "/").split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if parts:
                parts.pop()
            continue
        parts.append(seg)
    return "/".join(parts)


class _Resolver:
    def __init__(self, root, file_set, source_roots, aliases):
        self.root = Path(root)
        self.files = file_set
        self.dirs = set()
        for f in file_set:
            p = PurePosixPath(f).parent
            while str(p) not in ("", ".") and p.parent != p:
                self.dirs.add(p.as_posix())
                p = p.parent
        self.source_roots = [_norm(s) for s in source_roots]
        self.aliases = aliases
        self._base_cache = {}

    def py_base(self, rel_file):
        """파일이 속한 최상위 패키지의 부모 폴더."""
        d = PurePosixPath(rel_file).parent
        key = d.as_posix()
        if key in self._base_cache:
            return self._base_cache[key]
        cur = d
        while cur.as_posix() not in ("", ".") and cur.parent != cur and (cur / "__init__.py").as_posix() in self.files:
            cur = cur.parent
        base = "" if cur.as_posix() == "." else cur.as_posix()
        self._base_cache[key] = base
        return base

    def _py_try(self, base, dotted):
        p = _norm((base + "/" if base else "") + dotted.replace(".", "/"))
        if p + ".py" in self.files:
            return p + ".py"
        if p + "/__init__.py" in self.files or p in self.dirs:
            return p + "/__init__.py"
        return None

    def py(self, rel_file, level, module, names):
        out = []
        if level:
            d = PurePosixPath(rel_file).parent
            for _ in range(level - 1):
                d = d.parent
            base = "" if d.as_posix() == "." else d.as_posix()
            targets = [(base, module)] if module else [(base, n) for n in names]
            for b, m in targets:
                r = self._py_try(b, m) if m else None
                if r:
                    out.append(r)
            if module:
                for n in names:
                    r = self._py_try(base, module + "." + n)
                    if r:
                        out.append(r)
            return out
        bases = [self.py_base(rel_file)] + self.source_roots + [""]
        for b in bases:
            r = self._py_try(b, module)
            if r:
                out.append(r)
                for n in names:
                    r2 = self._py_try(b, module + "." + n)
                    if r2:
                        out.append(r2)
                break
        return out

    def ts(self, rel_file, spec):
        cands = []
        if spec.startswith("."):
            cands.append(_norm(PurePosixPath(rel_file).parent.as_posix() + "/" + spec))
        else:
            for prefix, tdirs in self.aliases:
                if spec.startswith(prefix):
                    rest = spec[len(prefix):]
                    for t in tdirs:
                        cands.append(_norm(t + "/" + rest))
                    break
            if not cands:
                return None
        for c in cands:
            if c in self.files:
                return c
            for ext in _TS_EXTS:
                if c + ext in self.files:
                    return c + ext
            for ext in _TS_EXTS:
                if c + "/index" + ext in self.files:
                    return c + "/index" + ext
        return None


# ---------------------------------------------------------------- 지도 만들기

def _module_id(rel_file):
    d = PurePosixPath(rel_file).parent.as_posix()
    return "." if d in ("", ".") else d


def _source_roots(root):
    cfg = read_json(paths.harness_dir(root) / "config.json", {}) or {}
    roots = cfg.get("source_roots") or ["."]
    return [r for r in roots if (Path(root) / r).is_dir()] or ["."]


def scan(root, source_roots=None):
    """결정적 부분(파일·공개 표면·의존). LLM 없음."""
    root = Path(root)
    source_roots = source_roots or _source_roots(root)
    limit = config.get("bootstrap.max_files", root)
    files, truncated = [], False
    seen = set()
    for sr in source_roots:
        found, t = util.walk_files(root / sr, util.SOURCE_EXT, limit=limit)
        truncated = truncated or t
        for f in found:
            r = util.rel(root, f)
            if r not in seen:
                seen.add(r)
                files.append(r)
    # tsconfig 등 별칭 파일
    cfg_files = []
    for sr in source_roots:
        for name in ("tsconfig.json", "jsconfig.json"):
            p = root / sr / name
            if p.is_file():
                cfg_files.append(util.rel(root, p))
    file_set = set(files)
    resolver = _Resolver(root, file_set, source_roots, _ts_aliases(root, cfg_files))

    modules = {}
    for f in files:
        mid = _module_id(f)
        m = modules.setdefault(mid, {"id": mid, "files": [], "lines": 0, "surface": [], "deps": set(),
                                     "largest_file": None, "lang": set()})
        m["files"].append(f)
        text = util.read_text(root / f)
        n = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        m["lines"] += n
        if not m["largest_file"] or n > m["largest_file"]["lines"]:
            m["largest_file"] = {"path": f, "lines": n}
        ext = PurePosixPath(f).suffix.lower()
        stem = PurePosixPath(f).stem
        if ext in util.PY_EXT:
            m["lang"].add("python")
            surf = py_surface(text) or []
            if stem != "__init__":
                surf = [stem + "." + s for s in surf]
            m["surface"].extend(surf)
            for level, mod, names in py_imports(text):
                for target in resolver.py(f, level, mod, names):
                    tm = _module_id(target)
                    if tm != mid:
                        m["deps"].add(tm)
        elif ext in util.JS_EXT:
            m["lang"].add("ts" if ext in (".ts", ".tsx") else "js")
            surf = ts_surface(text)
            if stem != "index":
                surf = [stem + "." + s for s in surf]
            m["surface"].extend(surf)
            for spec in ts_imports(text):
                target = resolver.ts(f, spec)
                if target:
                    tm = _module_id(target)
                    if tm != mid:
                        m["deps"].add(tm)
        else:
            m["lang"].add(ext.lstrip("."))
    out = []
    for mid in sorted(modules):
        m = modules[mid]
        surface = sorted(set(m["surface"]))
        out.append({
            "id": mid,
            "files": len(m["files"]),
            "file_list": sorted(m["files"]),
            "lines": m["lines"],
            "largest_file": m["largest_file"],
            "lang": sorted(m["lang"]),
            "surface": surface,
            "surface_hash": hashlib.sha256("\n".join(surface).encode("utf-8")).hexdigest()[:16],
            "deps": sorted(d for d in m["deps"] if d in modules),
            "responsibility": "",
        })
    return {"source_roots": source_roots, "truncated": truncated, "modules": out}


def _llm_responsibilities(root, modules, progress=None):
    """책임 한 줄(LLM). 실패하면 빈 dict — 비워 둔다.
    묶음을 병렬로 묻고 전체 상한(map_budget_s)을 넘으면 새 묶음을 내지 않는다 — 남은 모듈은 비워 두고 다음 build가 다시 묻는다.
    코드 모듈·큰 모듈부터 묻는다(상한에 걸려도 위험 지도 윗줄이 먼저 채워지게)."""
    import time
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
    from vmc_harness.core import llm
    if not llm.available() or llm.nested():
        return {}
    batch = config.get("bootstrap.map_llm_batch", root)
    timeout = config.get("bootstrap.map_timeout_s", root)
    model = config.get("bootstrap.map_model", root)
    deadline = time.time() + config.get("bootstrap.map_budget_s", root)
    code = ("python", "ts", "js")
    modules = sorted(modules, key=lambda m: (not any(l in code for l in m.get("lang", [])), -m.get("lines", 0), m["id"]))
    chunks = [modules[i:i + batch] for i in range(0, len(modules), batch)]
    out = {}
    say = progress or (lambda _m: None)
    say("책임 한 줄 %d개 모듈 · 묶음 %d개" % (len(modules), len(chunks)))
    workers = config.get("bootstrap.map_llm_workers", root)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        todo, running, done = list(chunks), set(), 0
        while todo or running:
            while todo and len(running) < workers and time.time() < deadline:
                running.add(ex.submit(_ask_responsibilities, todo.pop(0), model, timeout))
            if not running:
                break
            finished, running = wait(running, return_when=FIRST_COMPLETED)
            for fu in finished:
                out.update(fu.result())
                done += 1
                say("책임 한 줄 %d/%d 묶음" % (done, len(chunks)))
        if todo:
            say("책임 한 줄: 상한으로 %d개 묶음은 비워 둠(다음 `vmc-harness map`이 채운다)" % len(todo))
    return out


def _ask_responsibilities(chunk, model, timeout):
    from vmc_harness.core import llm
    items = [{"id": m["id"], "files": [PurePosixPath(f).name for f in m["file_list"][:15]], "surface": m["surface"][:30]}
             for m in chunk]
    prompt = (
        "목표: 아래 모듈마다 '이 모듈이 맡은 일'을 한국어 한 줄(40자 안팎)로 쓴다. 파일 이름과 공개 이름만 보고 쓴다.\n"
        "출력 형식: JSON 객체 하나 {\"<모듈 id>\": \"한 줄\", ...}. 다른 글 없이.\n\n"
        + json.dumps(items, ensure_ascii=False))
    res = llm.claude_p(prompt, model=model, timeout=timeout)
    data = _extract_json(res["text"]) if res else None
    if not isinstance(data, dict):
        return {}
    return {k: v.strip().replace("\n", " ")[:120] for k, v in data.items() if isinstance(v, str)}


def _extract_json(text):
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    body = m.group(1) if m else text
    for opener, closer in (("{", "}"), ("[", "]")):
        s, e = body.find(opener), body.rfind(closer)
        if s >= 0 and e > s:
            try:
                return json.loads(body[s:e + 1])
            except ValueError:
                continue
    return None


def build(root, use_llm=True, source_roots=None, progress=None):
    """구조 지도를 다시 만든다 → `.harness/cache/map.json`·`map.md`. dict를 돌려준다."""
    root = Path(root)
    prev = load(root) or {}
    prev_by_id = {m["id"]: m for m in prev.get("modules", [])}
    data = scan(root, source_roots)
    need = []
    for m in data["modules"]:
        old = prev_by_id.get(m["id"])
        if old and old.get("surface_hash") == m["surface_hash"] and old.get("responsibility"):
            m["responsibility"] = old["responsibility"]  # 공개 표면이 그대로면 다시 묻지 않는다
        else:
            need.append(m)
    if use_llm and need:
        resp = _llm_responsibilities(root, need, progress=progress)
        for m in need:
            m["responsibility"] = resp.get(m["id"], "")
    data["generated_at"] = now_iso()
    data["head"] = gitutil.head(root)
    write_json(map_path(root), data)
    md_path(root).write_text(render_md(data), encoding="utf-8")
    return data


def render_md(data):
    lines = ["# 구조 지도", "",
             "> 자동 생성(`vmc-harness map`). 모듈 = 소스 루트 아래 폴더. 손으로 고치지 않는다 — 어느 커밋에서든 다시 만든다.",
             "", "- 만든 시각: %s" % data.get("generated_at", ""),
             "- 기준 커밋: %s" % (data.get("head") or "없음"),
             "- 소스 루트: %s" % ", ".join(data.get("source_roots", [])),
             "- 모듈 %d개%s" % (len(data.get("modules", [])), " (파일 상한으로 잘림)" if data.get("truncated") else ""),
             "", "| 모듈 | 책임 | 파일·줄 | 공개 표면 | 의존 |", "|---|---|---|---|---|"]
    for m in data.get("modules", []):
        surf = m["surface"]
        s = ", ".join(surf[:8]) + (" 외 %d" % (len(surf) - 8) if len(surf) > 8 else "")
        deps = m["deps"]
        d = ", ".join(deps[:6]) + (" 외 %d" % (len(deps) - 6) if len(deps) > 6 else "")
        lines.append("| `%s` | %s | %d·%d | %s | %s |" % (
            m["id"], (m.get("responsibility") or "").replace("|", "/"), m["files"], m["lines"],
            s.replace("|", "/"), d))
    return "\n".join(lines) + "\n"


def module_of(root, rel_path):
    """파일(프로젝트 루트 상대)이 속한 모듈 id. 지도에 없으면 가장 가까운 상위 모듈, 그래도 없으면 None."""
    data = load(root)
    if not data:
        return None
    ids = {m["id"] for m in data.get("modules", [])}
    p = PurePosixPath(rel_path.replace("\\", "/"))
    cur = p.parent
    while True:
        key = "." if cur.as_posix() in ("", ".") else cur.as_posix()
        if key in ids:
            return key
        if key == "." or cur.parent == cur:  # 절대경로는 '/'·'C:/'에서 멈춘다
            return None
        cur = cur.parent


def diff(old, new):
    """지도 diff — 모듈 추가·삭제, 공개 표면 변화, 의존 변화. 내부 구현만 바뀌면 빈 결과(의도)."""
    old_by = {m["id"]: m for m in (old or {}).get("modules", [])}
    new_by = {m["id"]: m for m in (new or {}).get("modules", [])}
    out = {"added": sorted(set(new_by) - set(old_by)), "removed": sorted(set(old_by) - set(new_by)),
           "surface_changed": [], "deps_changed": []}
    for mid in sorted(set(old_by) & set(new_by)):
        a, b = old_by[mid], new_by[mid]
        sa, sb = set(a.get("surface", [])), set(b.get("surface", []))
        if sa != sb:
            out["surface_changed"].append({"id": mid, "added": sorted(sb - sa), "removed": sorted(sa - sb)})
        da, db = set(a.get("deps", [])), set(b.get("deps", []))
        if da != db:
            out["deps_changed"].append({"id": mid, "added": sorted(db - da), "removed": sorted(da - db)})
    out["empty"] = not any(out[k] for k in ("added", "removed", "surface_changed", "deps_changed"))
    return out


# ---------------------------------------------------------------- 묶음 추적 (Harness-Task trailer)

def task_bundles(root, since=None):
    """커밋 trailer `Harness-Task` 단위 묶음.

    [{"task_id","title","date","commits":[...],"new_modules":[...],"edges":[[a,b]],"text"}]
    """
    root = Path(root)
    args = ["log", "--reverse", "--format=%x1e%H%x1f%cI%x1f%s%x1f%(trailers:key=Harness-Task,valueonly,separator=%x2C)",
            "--name-status", "--no-renames"]
    if since:
        args.append("--since=%s" % since)
    rc, out, _ = gitutil.git(root, *args, timeout=60)
    if rc != 0:
        return []
    tasks = {}
    order = []
    for chunk in out.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        head, _, body = chunk.partition("\n")
        parts = head.split("\x1f")
        if len(parts) < 4:
            continue
        sha, date, subject, trailer = parts[0], parts[1], parts[2], parts[3].strip()
        if not trailer:
            continue
        added = []
        for line in body.splitlines():
            bits = line.split("\t")
            if len(bits) >= 2 and bits[0].startswith("A"):
                added.append(bits[-1])
        for tid in [t.strip() for t in trailer.split(",") if t.strip()]:
            t = tasks.get(tid)
            if not t:
                t = tasks[tid] = {"task_id": tid, "commits": [], "subjects": [], "date": date, "added": []}
                order.append(tid)
            t["commits"].append(sha)
            t["subjects"].append(subject)
            t["added"].extend(added)
    current = load(root) or {}
    deps_by = {m["id"]: set(m.get("deps", [])) for m in current.get("modules", [])}
    results = []
    for tid in order:
        t = tasks[tid]
        first = t["commits"][0]
        cand = sorted({_module_id(f) for f in t["added"] if PurePosixPath(f).suffix.lower() in util.SOURCE_EXT})
        new_mods = []
        for mid in cand:
            if mid == ".":
                continue
            rc, out, _ = gitutil.git(root, "ls-tree", "-d", "--name-only", first + "^", "--", mid)
            if rc != 0 or not out.strip():  # 첫 커밋 직전에 없던 폴더
                new_mods.append(mid)
        edges = []
        for a in new_mods:
            for b in sorted(deps_by.get(a, set())):
                if b in new_mods:
                    edges.append([a, b])
        title = t["subjects"][0]
        st, rec = util.optional_call([("vmc_harness.task.state", "by_id")], str(root), tid)
        if st == "ok" and isinstance(rec, dict):
            title = ((rec.get("declare") or {}).get("intent")) or title
        results.append({"task_id": tid, "title": title, "date": t["date"], "commits": t["commits"],
                        "new_modules": new_mods, "edges": edges,
                        "text": bundle_text(title, t["date"], new_mods, edges)})
    return results


def bundle_text(title, date, new_mods, edges):
    d = (date or "")[5:10].replace("-", "/").lstrip("0")
    head = "기능 %s 추가(%s)" % (title, d)
    if not new_mods:
        return head + ": 새 모듈 없음"
    names = "·".join(PurePosixPath(m).name for m in new_mods)
    s = "%s: 모듈 %d개 생김 %s" % (head, len(new_mods), names)
    if edges:
        s += ", 의존 " + ", ".join("%s→%s" % (PurePosixPath(a).name, PurePosixPath(b).name) for a, b in edges)
    return s
