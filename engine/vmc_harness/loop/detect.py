"""클렌징 후보 검출 도구 (spec 06) — 모델이 쓸 수 있는 도구(필수 아님). `vmc-harness loop detect <종류>`.

- unreferenced: 참조 0 코드·파일. Python은 표준 `ast` + 레포 전체 단어 검색(문자열·설정에서 이름이 보이면 참조로 친다 — 보수적).
  TS/JS는 고객 `node_modules/typescript` 컴파일러 API를 쓰는 엔진 내장 node 스크립트(`loop/node/ts_scan.js`). 고객 PC 추가 설치 없음
- duplicates: 이름·공백·리터럴을 지운 정규화 AST 해시가 같은 함수 묶음
- giant: 거대 파일·함수(줄 수)
- wander: 에이전트가 헤맨 파일(세션 기록의 파일별 도구 호출 집중도 — sessions 영역 파서)
"""

import ast
import copy
import fnmatch
import hashlib
import json
import re
import shutil
from pathlib import Path

from vmc_harness.core import config
from vmc_harness.core.gitutil import git
from vmc_harness.core.project import load_config
from vmc_harness.core.proc import run

PY_EXT = (".py",)
TS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
SKIP_PARTS = ("node_modules", ".harness", ".git", "dist", "build", ".next", "__pycache__", "vendor", ".venv", "venv")
ENTRY_NAMES = ("__main__.py", "__init__.py", "setup.py", "manage.py", "conftest.py", "wsgi.py", "asgi.py")
TS_ENTRY_RE = re.compile(r"(^|/)(app|pages)/|(^|/)(page|layout|route|middleware|loading|error|not-found|template|default)\.(t|j)sx?$|"
                         r"\.config\.|\.d\.ts$|(^|/)index\.(t|j)sx?$")
TEST_RE = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*\.py$|_test\.py$|\.(test|spec)\.(t|j)sx?$")
NODE_SCRIPT = Path(__file__).resolve().parent / "node" / "ts_scan.js"


def is_test_path(rel):
    return bool(TEST_RE.search(rel))


def source_files(root, exts=None):
    """git 추적 파일 중 source_roots 안, 제외 폴더 밖. 상대 경로(/ 구분)."""
    rc, out, _ = git(root, "ls-files", "-z")
    files = [f for f in out.split("\0") if f] if rc == 0 else []
    roots = load_config(root).get("source_roots") or ["."]
    roots = [r.strip("/").strip("\\") for r in roots]
    res = []
    for f in files:
        f = f.replace("\\", "/")
        if any(part in SKIP_PARTS for part in f.split("/")):
            continue
        if not any(r in ("", ".") or f == r or f.startswith(r + "/") for r in roots):
            continue
        if exts and not f.endswith(tuple(exts)):
            continue
        if (Path(root) / f).is_file():
            res.append(f)
    return sorted(res)


def _read(root, rel):
    try:
        return (Path(root) / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


# ---- Python 심볼 ----

def py_symbols(text):
    """[(qualname, node)] 최상위 함수·클래스와 클래스 메서드."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append((n.name, n))
            if isinstance(n, ast.ClassDef):
                for m in n.body:
                    if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        out.append(("%s.%s" % (n.name, m.name), m))
    return out


def find_py_symbol(text, symbol):
    for q, n in py_symbols(text):
        if q == symbol:
            return n
    return None


def symbol_source(root, rel, symbol):
    """앵커(경로+심볼)의 현재 소스. 심볼 없이 파일이면 파일 전체. 없으면 None."""
    text = _read(root, rel)
    if text is None:
        return None
    if not symbol:
        return text
    if rel.endswith(PY_EXT):
        n = find_py_symbol(text, symbol)
        if n is None:
            return None
        return ast.get_source_segment(text, n) or ""
    return ts_symbol_text(text, symbol)


def symbol_lines(root, rel, symbol):
    """(시작, 끝) 줄 번호(1부터). 없으면 None."""
    text = _read(root, rel)
    if text is None:
        return None
    if not symbol:
        return (1, len(text.splitlines()))
    if rel.endswith(PY_EXT):
        n = find_py_symbol(text, symbol)
        if n is None:
            return None
        start = min([n.lineno] + [d.lineno for d in getattr(n, "decorator_list", [])])
        return (start, getattr(n, "end_lineno", n.lineno))
    span = _ts_span(text, symbol)
    return span


_TS_DEF = r"(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:function\s*\*?\s*|class\s+|(?:const|let|var)\s+|interface\s+|type\s+|enum\s+)%s\b"


def _ts_span(text, symbol):
    name = symbol.split(".")[-1]
    m = re.search(_TS_DEF % re.escape(name), text)
    if not m:
        return None
    start_line = text.count("\n", 0, m.start()) + 1
    i = m.end()
    depth, seen = 0, False
    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
            seen = True
        elif ch == "}":
            depth -= 1
            if seen and depth == 0:
                break
        elif ch == ";" and not seen and depth == 0:
            break
        elif ch == "\n" and not seen and depth == 0 and re.match(r"\s*\n\s*(export|const|function|class|let|var|type|interface)\b", text[i:i + 40]):
            break
        i += 1
    end_line = text.count("\n", 0, i) + 1
    return (start_line, end_line)


def ts_symbol_text(text, symbol):
    span = _ts_span(text, symbol)
    if not span:
        return None
    lines = text.splitlines()
    return "\n".join(lines[span[0] - 1:span[1]])


# ---- 참조 ----

def word_refs(root, name, files=None, exclude=None):
    """레포 추적 파일 전체에서 단어 `name`이 나오는 (경로, 줄번호) 목록. exclude=(경로, 시작, 끝) 범위는 뺀다."""
    pat = re.compile(r"(?<![A-Za-z0-9_$])%s(?![A-Za-z0-9_$])" % re.escape(name))
    out = []
    rc, lsout, _ = git(root, "ls-files", "-z")
    allf = files if files is not None else ([f for f in lsout.split("\0") if f] if rc == 0 else [])
    for rel in allf:
        rel = rel.replace("\\", "/")
        if any(part in ("node_modules", ".git") for part in rel.split("/")):
            continue
        if rel.startswith(".harness/"):
            continue
        text = _read(root, rel)
        if text is None or name not in text:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if pat.search(line):
                if exclude and rel == exclude[0] and exclude[1] <= i <= exclude[2]:
                    continue
                out.append((rel, i))
    return out


def py_unreferenced_symbol(root, rel, symbol):
    text = _read(root, rel)
    if text is None:
        return False
    n = find_py_symbol(text, symbol)
    if n is None:
        return False
    name = symbol.split(".")[-1]
    if name.startswith("__") or name.startswith("test") or is_test_path(rel) or name in ("main", "setUp", "tearDown"):
        return False
    span = symbol_lines(root, rel, symbol)
    # 정의 안에서의 자기 호출(재귀)은 참조로 치지 않는다
    return not word_refs(root, name, exclude=(rel, span[0], span[1]))


def _module_names(rel):
    p = rel[:-3] if rel.endswith(".py") else rel
    parts = p.split("/")
    names = {parts[-1]}
    for i in range(len(parts)):
        names.add(".".join(parts[i:]))
    return names


def py_unreferenced_file(root, rel):
    if not rel.endswith(".py") or Path(rel).name in ENTRY_NAMES or is_test_path(rel):
        return False
    if not (Path(root) / rel).is_file():
        return False
    stem = Path(rel).stem
    refs = [r for r in word_refs(root, stem) if r[0] != rel]
    return not refs


def unreferenced(root):
    """[{"path","symbol","lang","kind":"symbol|file"}]"""
    out = []
    for rel in source_files(root, PY_EXT):
        if is_test_path(rel):
            continue
        if py_unreferenced_file(root, rel):
            out.append({"path": rel, "symbol": None, "lang": "python", "kind": "file"})
            continue
        text = _read(root, rel) or ""
        for q, _ in py_symbols(text):
            if py_unreferenced_symbol(root, rel, q):
                out.append({"path": rel, "symbol": q, "lang": "python", "kind": "symbol"})
    ts = ts_scan(root)
    if ts and not ts.get("error"):
        for u in ts.get("unreferenced") or []:
            out.append(dict(u, lang="ts"))
    elif ts and ts.get("error"):
        out.append({"error": "ts: %s" % ts["error"]})
    return out


# ---- TS 스캔 (엔진 내장 node 스크립트, 고객 typescript) ----

def ts_scan(root, files=None):
    """node 스크립트 결과 dict 또는 None(TS 파일 없음). 실패면 {"error": ...}."""
    ts_files = files or [f for f in source_files(root, TS_EXT) if not f.endswith(".d.ts")]
    if not ts_files:
        return None
    node = shutil.which("node")
    if not node:
        return {"error": "node 없음"}
    payload = json.dumps({"root": str(Path(root).resolve()), "files": ts_files,
                          "entry_re": TS_ENTRY_RE.pattern, "test_re": TEST_RE.pattern})
    rc, out, err = run([node, str(NODE_SCRIPT)], cwd=str(root), input_text=payload,
                       timeout=config.get("loop.find_timeout_s", root))
    if rc != 0:
        return {"error": (err or out or "node 실패").strip()[:300]}
    try:
        return json.loads(out)
    except ValueError:
        return {"error": "node 출력 형식 오류"}


# ---- 중복 함수 (정규화 AST 해시) ----

class _Norm(ast.NodeTransformer):
    def visit_Name(self, node):
        return ast.copy_location(ast.Name(id="_", ctx=node.ctx), node)

    def visit_arg(self, node):
        node.arg = "_"
        node.annotation = None
        return node

    def visit_Constant(self, node):
        return ast.copy_location(ast.Constant(value=type(node.value).__name__), node)

    def visit_Attribute(self, node):
        self.generic_visit(node)
        node.attr = "_"
        return node

    def visit_FunctionDef(self, node):
        node.name = "_"
        node.returns = None
        node.decorator_list = []
        if node.body and isinstance(node.body[0], ast.Expr) and isinstance(getattr(node.body[0], "value", None), ast.Constant) \
                and isinstance(node.body[0].value.value, str):
            node.body = node.body[1:] or [ast.Pass()]
        self.generic_visit(node)
        return node

    visit_AsyncFunctionDef = visit_FunctionDef


def norm_hash(node):
    n = _Norm().visit(copy.deepcopy(node))
    return hashlib.sha1(ast.dump(n, annotate_fields=False, include_attributes=False).encode("utf-8")).hexdigest()[:16]


def _node_size(node):
    return sum(1 for _ in ast.walk(node))


def module_of(root, rel):
    try:
        from vmc_harness.bootstrap.structmap import module_of as f
        m = f(root, rel)
        if m:
            return m
    except (ImportError, AttributeError):
        pass
    except Exception:
        pass
    parent = str(Path(rel).parent).replace("\\", "/")
    return parent if parent != "." else ""


def duplicates(root):
    """[{"hash","members":[{"path","symbol"}],"cross_module":bool,"lang"}]"""
    groups = {}
    min_nodes = config.get("loop.dup_min_nodes", root)
    for rel in source_files(root, PY_EXT):
        if is_test_path(rel):
            continue
        text = _read(root, rel) or ""
        for q, n in py_symbols(text):
            if isinstance(n, ast.ClassDef) or _node_size(n) < min_nodes:
                continue
            groups.setdefault(("python", norm_hash(n)), []).append({"path": rel, "symbol": q})
    ts = ts_scan(root)
    if ts and not ts.get("error"):
        min_lines = config.get("loop.dup_min_lines", root)
        texts = {}
        for f in ts.get("functions") or []:
            if f.get("lines", 0) < min_lines:
                continue
            if f["path"] not in texts:
                texts[f["path"]] = (_read(root, f["path"]) or "").splitlines()
            body = "\n".join(texts[f["path"]][f["start"] - 1:f["end"]])
            groups.setdefault(("ts", ts_text_hash(body)), []).append({"path": f["path"], "symbol": f["symbol"]})
    out = []
    for (lang, h), members in groups.items():
        if len(members) < 2:
            continue
        mods = {module_of(root, m["path"]) for m in members}
        out.append({"hash": h, "lang": lang, "members": members, "cross_module": len(mods) > 1})
    return sorted(out, key=lambda g: -len(g["members"]))


def symbol_norm_hash(root, rel, symbol):
    if rel.endswith(PY_EXT):
        n = find_py_symbol(_read(root, rel) or "", symbol)
        return norm_hash(n) if n is not None else None
    txt = symbol_source(root, rel, symbol)
    return ts_text_hash(txt) if txt else None


def ts_text_hash(txt):
    """TS 정규화 해시(주석·식별자·리터럴·공백 제거). 검출(duplicates)과 재확인(dup 질의)이 같은 함수를 쓴다."""
    s = re.sub(r"//[^\n]*|/\*.*?\*/", "", txt, flags=re.S)
    s = re.sub(r"(['\"`])(?:\\.|(?!\1).)*\1", "S", s, flags=re.S)
    s = re.sub(r"\b\d+(\.\d+)?\b", "N", s)
    s = re.sub(r"\b[A-Za-z_$][A-Za-z0-9_$]*\b", lambda m: m.group(0) if m.group(0) in _TS_KEYWORDS else "I", s)
    s = re.sub(r"\s+", "", s)
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


_TS_KEYWORDS = set("""function return if else for while do switch case break continue const let var new class extends
async await try catch finally throw typeof instanceof in of this null undefined true false import export default""".split())


# ---- 거대 파일·함수 ----

def giant(root):
    fmax = config.get("loop.giant_file_lines", root)
    gmax = config.get("loop.giant_function_lines", root)
    out = []
    for rel in source_files(root, PY_EXT + TS_EXT):
        text = _read(root, rel)
        if text is None:
            continue
        n = len(text.splitlines())
        if n > fmax:
            out.append({"path": rel, "symbol": None, "lines": n, "kind": "file"})
        if rel.endswith(PY_EXT):
            for q, node in py_symbols(text):
                if isinstance(node, ast.ClassDef):
                    continue
                ln = getattr(node, "end_lineno", node.lineno) - node.lineno + 1
                if ln > gmax:
                    out.append({"path": rel, "symbol": q, "lines": ln, "kind": "function"})
    ts = ts_scan(root)
    if ts and not ts.get("error"):
        for f in ts.get("functions") or []:
            if f.get("lines", 0) > gmax:
                out.append({"path": f["path"], "symbol": f["symbol"], "lines": f["lines"], "kind": "function"})
    return sorted(out, key=lambda x: -x["lines"])


# ---- 헤맨 파일 ----

def wander(root):
    """{rel_path: 도구 호출 수} — 최근 세션들에서 파일별 도구 호출 집중도. 세션을 못 읽으면 {}."""
    try:
        from vmc_harness import sessions
    except ImportError:
        return {}
    counts = {}
    rootp = Path(root).resolve()
    try:
        found = sessions.find(str(root), limit=config.get("loop.wander_max_sessions", root))
    except Exception:
        return {}
    for agent, path in found:
        try:
            s = sessions.parse(path, agent)
        except Exception:
            continue
        if not getattr(s, "measured", True):
            continue
        for c in getattr(s, "tool_calls", []) or []:
            for p in c.get("paths") or []:
                rel = _rel(rootp, p)
                if rel:
                    counts[rel] = counts.get(rel, 0) + 1
    minc = config.get("loop.wander_min_calls", root)
    return {k: v for k, v in counts.items() if v >= minc}


def _rel(rootp, p):
    pp = Path(p)
    if not pp.is_absolute():
        return str(p).replace("\\", "/")
    try:
        return pp.resolve().relative_to(rootp).as_posix()
    except (ValueError, OSError):
        return None


def wander_score(scores, rel):
    """경로·그 폴더의 헤맨 정도(파일 자신 + 같은 폴더 합의 절반)."""
    own = scores.get(rel, 0)
    parent = str(Path(rel).parent).replace("\\", "/")
    near = sum(v for k, v in scores.items() if k != rel and str(Path(k).parent).replace("\\", "/") == parent)
    return own + near / 2.0


def matches_glob(rel, globs):
    return any(fnmatch.fnmatch(rel, g) for g in globs or [])
