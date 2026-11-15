"""과설계 점검 (spec 03 §과설계 점검) — 작업 종료 때, **선언 대비 diff**로 본다("평소 수준 대비"는 쓰지 않는다).

기계 신호(모든 작업):
- new_file_out_of_scope   선언(new)에 없던 새 파일
- new_module_out_of_scope 선언에 없던 새 모듈(착수 시점에 없던 디렉터리)
- new_dependency          의존성 파일 diff(package.json·pyproject.toml·requirements*.txt)
- unreferenced_public     새 공개 함수·export 중 참조 0 (Python ast, TS/JS 정규식)
- single_impl_abstraction 구현 하나뿐인 새 추상화(Protocol·ABC·interface) — bootstrap facts `conventions`의 기존 관례 예외
- near_duplicate_function 기존 함수와 거의 같은 새 함수(정규화 AST 해시·유사도, Python)
LLM 신호(중·대만, 새 컨텍스트, 목표만):
- unrequested_option      요청에 없는 옵션·설정·기능 — 이번 작업 지시(착수 직전 사람 메시지부터)와 착수 선언 대비.
                          기존 관례를 따른 새 파일(같은 폴더·같은 이름 끝이 이미 여럿)·같은 폴더 기존 파일에 있던 절·
                          착수 때 이미 있던 필드를 지목한 신호는 뺀다

신호가 잡히면 축소 검토 1회만(handlers.on_stop). 메시지는 무엇이 잡혔는지만(01 P6).
새 파일·새 모듈(NOTE_ONLY)은 막지 않는다 — 완료 보고 "범위 밖 변경"에 이미 나온다. 실측(11-11~16): 이 신호만으로 막은 8건은
전부 재선언만 하고 결과물이 그대로였고, 결과물을 바꾼 차단 9건은 모두 다른 신호였다.
"""

import ast
import difflib
import fnmatch
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath

from vmc_harness.core import config, gitutil
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.task import scope, state as st

PY = (".py",)
TS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
DEP_FILES = ("package.json", "pyproject.toml", "requirements*.txt", "requirements/*.txt")
# 프레임워크가 이름으로 부르는 export(참조 0이어도 정상)
FRAMEWORK_EXPORTS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "metadata",
                     "generateMetadata", "generateStaticParams", "config", "revalidate", "dynamic",
                     "middleware", "loader", "action", "runtime"}
PY_ENTRY = {"main", "setup", "teardown", "setUp", "tearDown", "setUpClass", "tearDownClass"}

LABEL = {
    "new_file_out_of_scope": "선언에 없던 새 파일",
    "new_module_out_of_scope": "선언에 없던 새 모듈",
    "new_dependency": "새 의존성",
    "unreferenced_public": "참조 없는 새 공개 함수",
    "single_impl_abstraction": "구현 하나뿐인 새 추상화",
    "near_duplicate_function": "기존 함수와 거의 같은 새 함수",
    "unrequested_option": "요청에 없는 옵션·기능",
}
# 막지 않고 완료 보고(범위 밖 변경)로만 보이는 신호
NOTE_ONLY = ("new_file_out_of_scope", "new_module_out_of_scope")

OD_SYSTEM = (
    "목표: 이번 작업의 요청 대비 과한 것을 찾아라 — 요청에 없는 옵션·설정·기능·확장 지점.\n"
    "참고: [이번 작업을 시작한 사람 지시]와 [작업 중 사람 지시]가 요청이다. 사람 지시가 짧은 승인('ㄱㄱ' 등)이면 "
    "[사람이 승인한 계획 — 직전 어시스턴트 제안]도 요청이다(그 계획에 적힌 단계는 과한 것이 아니다). [이전 대화]는 맥락일 뿐 이번 작업의 요청이 아니다. "
    "[착수 선언]은 작업 시작 때 사람에게 보인 범위다 — 짧은 지시(링크만·'ㄱㄱ' 등)를 해석할 때 쓰고, 사람 지시와 어긋나면 사람 지시가 우선이다. "
    "그리고 이번 작업의 diff.\n"
    "제약: 요청을 이루는 데 필요한 것은 과한 것이 아니다. 요청을 하다 생긴 결함을 바로잡은 것과 범위 안 파일의 기존 값 조정"
    "(스타일 수치·간격·줄바꿈 등)은 옵션·기능이 아니다 — 신호는 새 옵션·플래그·설정·분기·기능만. 확신이 없으면 넣지 않는다.\n"
    '출력 형식: JSON 한 줄만. {"signals":[{"what":"<무엇이 과한지 한 구절>","path":"<파일>"}]} 없으면 {"signals":[]}'
)


# ---- git 도움 ----

def _show(root, ref, path):
    if not ref:
        return None
    rc, out, _ = gitutil.git(root, "show", "%s:%s" % (ref, path))
    return out if rc == 0 else None


def _base_files(root, ref):
    if not ref:
        return set()
    rc, out, _ = gitutil.git(root, "ls-tree", "-r", "--name-only", "-z", ref)
    return set(gitutil.split_z(out)) if rc == 0 else set()


def _read(root, rel):
    p = Path(root) / rel
    try:
        return p.read_text(encoding="utf-8", errors="replace") if p.is_file() else None
    except OSError:
        return None


def _repo_files(root, exts):
    files = set()
    rc, out, _ = gitutil.git(root, "ls-files", "--cached", "--others", "--exclude-standard")
    if rc == 0:
        for l in out.splitlines():
            l = l.strip()
            if l.endswith(exts) and (Path(root) / l).is_file():
                files.add(l)
    return sorted(files)


def _ref_count(root, name, exclude_file=None, exclude_line=None):
    """이름이 단어로 나오는 줄 수(정의 줄 제외). 저장소의 추적·미추적 파일 대상."""
    rc, out, _ = gitutil.git(root, "grep", "-n", "-w", "-I", "--untracked", "-F", "-e", name)
    if rc not in (0, 1):
        return None
    n = 0
    for line in out.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        f, ln, text = parts
        if f == exclude_file and str(ln) == str(exclude_line):
            continue
        if f.startswith(scope.HARNESS_PREFIX):
            continue
        n += 1
    return n


# ---- 의존성 ----

_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.\-]*)")


def _deps_package_json(text):
    try:
        d = json.loads(text or "{}")
    except ValueError:
        return set()
    out = set()
    for k in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        if isinstance(d.get(k), dict):
            out.update(d[k].keys())
    return out


def _deps_requirements(text):
    out = set()
    for line in (text or "").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        m = _REQ_NAME.match(line)
        if m:
            out.add(m.group(1).lower())
    return out


def _deps_pyproject(text):
    out = set()
    text = text or ""
    for m in re.finditer(r"(?m)^\s*(?:dependencies|[A-Za-z0-9_\-]+)\s*=\s*\[((?:[^\]\"']|\"[^\"]*\"|'[^']*')*)\]", text):
        head = text[m.start():m.start() + 200].split("=", 1)[0].strip()
        if head != "dependencies" and not _in_section(text, m.start(), "project.optional-dependencies"):
            continue
        for s in re.findall(r"[\"']([^\"']+)[\"']", m.group(1)):
            n = _REQ_NAME.match(s)
            if n:
                out.add(n.group(1).lower())
    section = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s.strip("[]").strip()
            continue
        if section and re.match(r"tool\.poetry\.(?:group\.[^.]+\.)?(?:dev-)?dependencies$", section):
            m = _REQ_NAME.match(s)
            if m and "=" in s and m.group(1).lower() != "python":
                out.add(m.group(1).lower())
    return out


def _in_section(text, pos, name):
    sec = None
    for m in re.finditer(r"(?m)^\s*\[([^\]]+)\]\s*$", text[:pos]):
        sec = m.group(1).strip()
    return sec == name


def _deps(path, text):
    name = PurePosixPath(path).name
    if name == "package.json":
        return _deps_package_json(text)
    if name == "pyproject.toml":
        return _deps_pyproject(text)
    return _deps_requirements(text)


def _is_dep_file(path):
    name = PurePosixPath(path).name
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(path, p) for p in DEP_FILES)


def _repo_deps(root, base, base_files):
    """착수 시점 저장소의 의존성 이름 — {"package.json": {...}, "py": {...}}(Python 파일 형식끼리는 한 묶음)."""
    out = {"package.json": set(), "py": set()}
    for f in base_files or ():
        if "node_modules/" not in f and _is_dep_file(f):
            out[_dep_kind(f)] |= _deps(f, _show(root, base, f) or "")
    return out


def _dep_kind(path):
    return "package.json" if PurePosixPath(path).name == "package.json" else "py"


# ---- Python AST ----

def _parse_py(text):
    try:
        return ast.parse(text or "")
    except (SyntaxError, ValueError):
        return None


def _top_defs(tree):
    out = {}
    if tree is None:
        return out
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[n.name] = n
    return out


def _all_funcs(tree):
    out = []
    if tree is None:
        return out
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(n)
    return out


def _qual_funcs(tree):
    """{"Class.method"|"func": node} — 모듈 최상위와 클래스 안 함수."""
    out = {}
    if tree is None:
        return out

    def walk(body, prefix):
        for n in body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[prefix + n.name] = n
                walk(n.body, prefix + n.name + ".")
            elif isinstance(n, ast.ClassDef):
                walk(n.body, prefix + n.name + ".")
    walk(tree.body, "")
    return out


def _base_names(cls):
    names = []
    for b in cls.bases:
        if isinstance(b, ast.Name):
            names.append(b.id)
        elif isinstance(b, ast.Attribute):
            names.append(b.attr)
        elif isinstance(b, ast.Subscript):
            v = b.value
            names.append(v.id if isinstance(v, ast.Name) else getattr(v, "attr", ""))
    for kw in cls.keywords:
        if kw.arg == "metaclass":
            v = kw.value
            names.append(v.id if isinstance(v, ast.Name) else getattr(v, "attr", ""))
    return names


def _abstraction_kind(cls):
    bases = _base_names(cls)
    if "Protocol" in bases:
        return "protocol"
    if "ABC" in bases or "ABCMeta" in bases:
        return "abc"
    for n in ast.walk(cls):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                name = d.id if isinstance(d, ast.Name) else getattr(d, "attr", "")
                if name == "abstractmethod":
                    return "abc"
    return None


class _Norm(ast.NodeTransformer):
    """식별자·상수 값·docstring을 지워 구조만 남긴다."""

    def visit_Name(self, node):
        return ast.copy_location(ast.Name(id="_", ctx=node.ctx), node)

    def visit_arg(self, node):
        node.arg = "_"
        node.annotation = None
        return node

    def visit_Constant(self, node):
        return ast.copy_location(ast.Constant(value=type(node.value).__name__), node)

    def _strip_doc(self, node):
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]
        return node

    def visit_FunctionDef(self, node):
        node = self._strip_doc(node)
        node.name = "_"
        node.decorator_list = []
        node.returns = None
        self.generic_visit(node)
        return node

    visit_AsyncFunctionDef = visit_FunctionDef


def _norm_dump(fn):
    import copy
    try:
        n = _Norm().visit(copy.deepcopy(fn))
        return ast.dump(n, annotate_fields=False, include_attributes=False)
    except Exception:
        return None


def _node_count(fn):
    return sum(1 for _ in ast.walk(fn))


def _tokens(dump):
    return re.findall(r"[A-Za-z_]+|[^\sA-Za-z_]", dump or "")


# ---- TS/JS ----

_TS_EXPORT = re.compile(
    r"^\s*export\s+(?:declare\s+)?(?:async\s+)?(?:function\*?|const|let|var|class|interface|type|enum|abstract\s+class)\s+([A-Za-z_$][\w$]*)",
    re.M)
_TS_ABSTRACT = re.compile(r"^\s*(?:export\s+)?(?:declare\s+)?(interface|abstract\s+class)\s+([A-Za-z_$][\w$]*)", re.M)


def _ts_exports(text):
    return {m.group(1): text[:m.start()].count("\n") + 1 for m in _TS_EXPORT.finditer(text or "")}


def _ts_abstractions(text):
    return {m.group(2): ("interface" if m.group(1) == "interface" else "abstract")
            for m in _TS_ABSTRACT.finditer(text or "")}


# ---- 관례 예외 ----

def _conventions_text(root):
    try:
        from vmc_harness.bootstrap.facts import load
    except ImportError:
        return ""
    try:
        conv = (load(root) or {}).get("conventions") or []
    except Exception:
        return ""
    parts = []
    for c in conv if isinstance(conv, list) else [conv]:
        parts.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
    return " ".join(parts).lower()


def _convention_allows(conv_text, kind):
    if not conv_text:
        return False
    keys = {"protocol": ("protocol",), "abc": ("abc", "abstract"), "interface": ("interface",),
            "abstract": ("abstract",)}.get(kind, (kind,))
    return any(k in conv_text for k in keys)


# ---- 선언 대조(새 파일이 선언된 것인가) ----

# 경로 낱말을 가르는 글자(공백 말고) — 문장으로 쓴 선언에서 경로만 뽑는다
_TOKEN_SPLIT = re.compile(r"[\s,;()\[\]{}<>\"'`+|]+")
_EXT_TAIL = re.compile(r"^(.*\.[A-Za-z0-9]+)[^\x00-\x7f]+$")   # "page.md를"·"index.md에" → 조사 떼기
_HAS_EXT = re.compile(r"\.[A-Za-z0-9]+$")
_RANGE = re.compile(r"^(.*?)(\d+)~(\d+)(.*)$")                 # "fig1~5.html"
_BROAD = {"", ".", "*", "**", "**/*", "*/**", "/"}


def _nfc(s):
    import unicodedata
    return unicodedata.normalize("NFC", s)


def _rel_to_root(root, tok):
    """절대 경로면 프로젝트 루트 상대로(루트 밖이면 None). 상대 경로는 그대로."""
    import os
    if not tok.startswith("/") and not re.match(r"^[A-Za-z]:[\\/]", tok):
        return tok
    t = tok.replace("\\", "/")
    for base in {str(root), os.path.realpath(str(root))}:
        b = base.replace("\\", "/").rstrip("/") + "/"
        if t.startswith(b):
            return t[len(b):]
    return None


def path_tokens(root, entry):
    """선언 항목 하나(경로·glob·문장)에서 경로 낱말만 뽑아 루트 상대로. 경로 모양(/·확장자·glob 글자)이 아닌 낱말은
    루트에 그 이름의 폴더가 있을 때만 남긴다 — "문서"·"새 페이지" 같은 말이 모든 파일을 선언된 것으로 만들지 않게. "fig1~5.html"은 fig1.html…fig5.html로 푼다."""
    out = []
    for raw in _TOKEN_SPLIT.split(_nfc(str(entry or "").replace("\\", "/"))):
        t = raw.strip().rstrip(".:;!?")
        m = _EXT_TAIL.match(t)
        if m:
            t = m.group(1)
        t = _rel_to_root(root, t)
        if t is None:
            continue
        is_dir = t.endswith("/")
        t = scope.norm(t)
        if t in _BROAD or t.startswith("../"):
            continue
        if not ("/" in t or is_dir or _HAS_EXT.search(t) or any(ch in t for ch in "*?")):
            if not (Path(root) / t).is_dir():
                continue   # 경로 모양이 아닌 낱말 — 루트에 그 이름의 폴더가 있을 때만 경로로 본다
            is_dir = True
        r = _RANGE.match(t)
        if r and int(r.group(2)) <= int(r.group(3)) \
                and int(r.group(3)) - int(r.group(2)) < int(config.get("task.declare_range_max", root)):
            out.extend("%s%d%s" % (r.group(1), i, r.group(4)) for i in range(int(r.group(2)), int(r.group(3)) + 1))
            continue
        out.append(t + "/" if is_dir and not any(ch in t for ch in "*?") else t)
    return out


def _token_hit(path, tok):
    """루트 상대 경로 path가 낱말 tok(루트 상대, 다른 선언 폴더 상대, 파일 이름만)에 들어맞는가."""
    if tok.endswith("/"):
        d = tok.rstrip("/")
        return path.startswith(d + "/") or ("/" + d + "/") in ("/" + path)
    if any(ch in tok for ch in "*?["):
        return fnmatch.fnmatch(path, tok) or fnmatch.fnmatch(path, tok + "/*") or fnmatch.fnmatch(path, "*/" + tok)
    return path == tok or path.startswith(tok + "/") or path.endswith("/" + tok) or ("/" + tok + "/") in ("/" + path)


def _scope_hit(path, tok):
    """선언 범위(scope)의 폴더·glob 아래인가 — 루트 상대로만(파일 이름만 맞추기는 하지 않는다)."""
    if tok.endswith("/"):
        return path.startswith(tok)
    if any(ch in tok for ch in "*?["):
        return fnmatch.fnmatch(path, tok) or fnmatch.fnmatch(path, tok + "/*")
    return path == tok or path.startswith(tok + "/")


def declared_matcher(root, declare):
    """path → 선언된 새 것인가. 받는 것: 새로 만들 것(new)의 경로·폴더·glob·문장 속 경로(루트 상대·다른 선언 폴더 상대·파일 이름),
    건드릴 범위(scope)의 폴더·glob 아래. 레포 전체("."·"**")를 범위로 적은 선언은 새 파일을 선언한 것으로 치지 않는다."""
    declare = declare or {}
    new_toks = [t for e in declare.get("new") or [] for t in path_tokens(root, e)]
    scope_toks = [t for e in declare.get("scope") or [] for t in path_tokens(root, e)]
    new_raw = {"scope": [x for x in (scope.norm(y) for y in declare.get("new") or []) if x not in _BROAD]}

    def hit(path):
        p = _nfc(scope.norm(path))
        if any(_token_hit(p, t) for t in new_toks) or any(_scope_hit(p, t) for t in scope_toks):
            return True
        return scope.in_scope(root, new_raw, p)
    return hit


# ---- 기계 신호 ----

def machine_signals(root, task, files=None):
    """[{"kind","path","name"?,"detail"?}]"""
    declare = task.get("declare") or {}
    base = (task.get("base") or {}).get("head")
    files = files if files is not None else scope.touched(root, task)
    base_files = _base_files(root, base)
    base_dirs = {str(PurePosixPath(f).parent) for f in base_files}
    for f in list(base_dirs):
        p = PurePosixPath(f)
        base_dirs.update(str(x) for x in p.parents)
    signals = []
    declared = declared_matcher(root, declare)
    existing = [f for f in files if (Path(root) / f).is_file()]
    # 새 파일 = 착수 스냅샷 때 없던 파일(HEAD에도, 착수 전 미커밋 변경에도 없음 — scope.existed_at_base와 같은 기준)
    pre = (task.get("base") or {}).get("dirty") or {}
    new_files = [f for f in existing if f not in base_files and pre.get(f) is None] if base else list(existing)

    # 새 파일·모듈
    seen_mod = set()
    for f in new_files:
        if declared(f):
            continue
        signals.append({"kind": "new_file_out_of_scope", "path": f})
        d = str(PurePosixPath(f).parent)
        if base and d not in (".", "") and d not in base_dirs:
            top = d
            for parent in PurePosixPath(d).parents:
                if str(parent) in (".", "") or str(parent) in base_dirs:
                    break
                top = str(parent)
            if top not in seen_mod and not declared(top + "/x"):
                seen_mod.add(top)
                signals.append({"kind": "new_module_out_of_scope", "path": top})

    # 새 의존성 — 저장소의 다른 의존성 파일(착수 시점)에 이미 있는 이름은 새 의존성이 아니다(새 앱의 react·react-dom 등)
    new_names = " ".join(declare.get("new") or []).lower()
    repo_deps = None
    for f in files:
        if not _is_dep_file(f):
            continue
        before = _deps(f, _show(root, base, f) or "")
        after = _deps(f, _read(root, f) or "")
        for name in sorted(after - before):
            if name.lower() in new_names:
                continue
            if repo_deps is None:
                repo_deps = _repo_deps(root, base, base_files)
            if name in repo_deps[_dep_kind(f)]:
                continue
            signals.append({"kind": "new_dependency", "path": f, "name": name})

    conv = None
    py_new_funcs = []
    for f in existing:
        if f.endswith(PY):
            now_tree = _parse_py(_read(root, f))
            old_tree = _parse_py(_show(root, base, f)) if f not in new_files else None
            now_defs, old_defs = _top_defs(now_tree), _top_defs(old_tree)
            is_test = PurePosixPath(f).name.startswith("test_") or "/tests/" in "/" + f
            for name, node in now_defs.items():
                if name in old_defs:
                    continue
                # 참조 0 공개 함수
                if not name.startswith("_") and not is_test and name not in PY_ENTRY \
                        and not getattr(node, "decorator_list", None):
                    refs = _ref_count(root, name, exclude_file=f, exclude_line=node.lineno)
                    if refs == 0:
                        signals.append({"kind": "unreferenced_public", "path": f, "name": name})
                # 구현 하나뿐인 추상화
                if isinstance(node, ast.ClassDef):
                    kind = _abstraction_kind(node)
                    if kind:
                        if conv is None:
                            conv = _conventions_text(root)
                        if not _convention_allows(conv, kind):
                            impls = _py_impl_count(root, name, kind)
                            if impls is not None and impls <= 1:
                                signals.append({"kind": "single_impl_abstraction", "path": f, "name": name,
                                                "detail": kind})
            old_names = set(_qual_funcs(old_tree))
            for qn, fn in _qual_funcs(now_tree).items():
                if qn in old_names:
                    continue
                d = _norm_dump(fn)
                if d:
                    py_new_funcs.append((f, fn, d))
        elif f.endswith(TS):
            now_text = _read(root, f) or ""
            old_text = _show(root, base, f) if f not in new_files else ""
            old_exp = set(_ts_exports(old_text or ""))
            for name, line in _ts_exports(now_text).items():
                if name in old_exp or name in FRAMEWORK_EXPORTS or "/test" in "/" + f or ".test." in f \
                        or ".spec." in f:
                    continue
                refs = _ref_count(root, name, exclude_file=f, exclude_line=line)
                if refs == 0:
                    signals.append({"kind": "unreferenced_public", "path": f, "name": name})
            old_abs = _ts_abstractions(old_text or "")
            for name, kind in _ts_abstractions(now_text).items():
                if name in old_abs:
                    continue
                if conv is None:
                    conv = _conventions_text(root)
                if _convention_allows(conv, kind):
                    continue
                impls = _ts_impl_count(root, name, kind)
                if impls == 1:
                    signals.append({"kind": "single_impl_abstraction", "path": f, "name": name, "detail": kind})

    if py_new_funcs:
        signals.extend(_near_duplicates(root, py_new_funcs, set(files)))
    return signals


def _py_impl_count(root, name, kind):
    """name을 상속하는 클래스 수. Protocol은 구조적 타이핑이라 이름에 name을 품은 클래스도 센다(InMemoryX·MssqlX 관례)."""
    rc, out, _ = gitutil.git(root, "grep", "-n", "-I", "--untracked", "-E", "^[[:space:]]*class[[:space:]]")
    if rc not in (0, 1):
        return None
    pat_base = re.compile(r"class\s+([A-Za-z_]\w*)\s*\(([^)]*)\)")
    pat_any = re.compile(r"class\s+([A-Za-z_]\w*)")
    n = 0
    for line in out.splitlines():
        parts = line.split(":", 2)
        if len(parts) < 3 or not parts[0].endswith(PY):
            continue
        text = parts[2]
        m = pat_base.search(text)
        if m and m.group(1) != name and re.search(r"\b%s\b" % re.escape(name), m.group(2)):
            n += 1
            continue
        if kind == "protocol":
            m2 = pat_any.search(text)
            if m2 and m2.group(1) != name and name in m2.group(1):
                n += 1
    return n


def _ts_impl_count(root, name, kind):
    word = "implements" if kind == "interface" else "extends"
    rc, out, _ = gitutil.git(root, "grep", "-n", "-I", "--untracked", "-F", "-e", word)
    if rc not in (0, 1):
        return None
    pat = re.compile(r"\b%s\b[^{]*\b%s\b" % (word, re.escape(name)))
    return sum(1 for l in out.splitlines() if l.split(":", 1)[0].endswith(TS) and pat.search(l.split(":", 2)[-1]))


def _near_duplicates(root, new_funcs, touched):
    threshold = config.get("task.dup_similarity", root)
    min_nodes = config.get("task.dup_min_nodes", root)
    max_files = config.get("task.dup_scan_max_files", root)
    candidates = [(f, fn, d) for f, fn, d in new_funcs if _node_count(fn) >= min_nodes]
    if not candidates:
        return []
    new_lines = {(f, fn.lineno) for f, fn, _ in new_funcs}
    existing = []  # (path, name, dump, hash)
    for rel in _repo_files(root, PY)[:max_files]:
        tree = _parse_py(_read(root, rel))
        for fn in _all_funcs(tree):
            if (rel, fn.lineno) in new_lines or _node_count(fn) < min_nodes:
                continue
            d = _norm_dump(fn)
            if not d:
                continue
            existing.append((rel, fn.name, d, hashlib.sha1(d.encode()).hexdigest()))
    by_hash = {}
    for e in existing:
        by_hash.setdefault(e[3], e)
    out = []
    for f, fn, d in candidates:
        h = hashlib.sha1(d.encode()).hexdigest()
        hit = by_hash.get(h)
        if not hit:
            toks = _tokens(d)
            best = None
            for e in existing:
                et = _tokens(e[2])
                if not et or min(len(toks), len(et)) / float(max(len(toks), len(et))) < threshold:
                    continue
                sm = difflib.SequenceMatcher(None, toks, et, autojunk=False)
                if sm.real_quick_ratio() < threshold or sm.quick_ratio() < threshold:
                    continue
                r = sm.ratio()
                if r >= threshold and (best is None or r > best[0]):
                    best = (r, e)
            hit = best[1] if best else None
        if hit:
            out.append({"kind": "near_duplicate_function", "path": f, "name": fn.name,
                        "detail": "%s:%s" % (hit[0], hit[1])})
    return out


# ---- LLM 신호(중·대) ----

def od_path(root, task_id):
    return st.task_path(root, task_id, ".od")


def spawn_llm(root, task_id, transcript_path=None, agent=None):
    """요청에 없는 옵션·기능 LLM 판정을 백그라운드로(Stop 훅 시간 상한 안에 못 끝나므로)."""
    from vmc_harness.task import pm
    write_json(od_path(root, task_id), {"status": "running"})
    args = [sys.executable, "-m", "vmc_harness", "task-bg", task_id, "--root", str(root), "--overdesign"]
    if transcript_path:
        args += ["--transcript", str(transcript_path)]
    if agent:
        args += ["--agent", agent]
    if pm.is_sync(root, agent):
        run_llm(root, task_id, transcript_path, agent)
        return "sync"
    try:
        from vmc_harness.core.proc import spawn_detached
        spawn_detached(args, cwd=str(root), env=pm._env())
        return "async"
    except Exception:
        run_llm(root, task_id, transcript_path, agent)
        return "sync"


def _split_prompts(prompts, since):
    """사람 메시지를 이번 작업 기준으로 나눈다 — (이전 대화, 이번 작업을 시작한 지시, 작업 중 지시).
    이번 작업을 시작한 지시 = 착수(since) 직전 마지막 사람 메시지. since가 없거나 못 읽으면 마지막 메시지."""
    from vmc_harness.core.clock import parse_iso
    texts = [(parse_iso(p.get("at")), p.get("text") or "") for p in prompts or []]
    if not texts:
        return [], "", []
    t0 = parse_iso(since) if since else None
    idx = len(texts) - 1
    if t0 is not None:
        before = [i for i, (t, _) in enumerate(texts) if t is not None and t <= t0]
        idx = before[-1] if before else 0
    return [x for _, x in texts[:idx]], texts[idx][1], [x for _, x in texts[idx + 1:]]


def _approved_plan(session, start_text, since, max_chars, short_max_chars):
    """이번 작업을 시작한 사람 지시가 짧은 승인("ㄱㄱ" 등)이면, 그 지시 바로 앞 사람 메시지 이후의 어시스턴트 글(직전 제안).
    계획은 어시스턴트가 적고 사람은 "ㄱㄱ"만 하는 흐름에서 요청 내용을 잃어 계획대로 한 일을 '요청 밖'으로 막았다(2026-11-15 현장)."""
    from vmc_harness.core.clock import parse_iso
    if not max_chars or len((start_text or "").strip()) > short_max_chars:
        return ""
    texts = getattr(session, "assistant_texts", None) or []
    prompts = [(parse_iso(p.get("at")), p.get("text") or "") for p in session.human_prompts or []]
    t0 = parse_iso(since) if since else None
    starts = [t for t, x in prompts if t is not None and x == start_text and (t0 is None or t <= t0)]
    if not starts:
        return ""
    t_start = starts[-1]
    before = [t for t, _ in prompts if t is not None and t < t_start]
    t_prev = before[-1] if before else None
    plan = [x.get("text") or "" for x in texts
            if (lambda t: t is not None and t < t_start and (t_prev is None or t > t_prev))(parse_iso(x.get("at")))]
    return "\n".join(plan)[-max_chars:]


def _prompts_text(transcript_path, agent, max_chars, since=None, prior_max_chars=0, plan_max_chars=0, short_max_chars=0):
    """판정에 넣을 사람 지시문. 세션 전체를 한 덩어리로 주면 앞선 다른 요청("브리핑해줘")을 이번 작업의 요청으로
    읽는다(2026-11-14 현장 — 회의록 적재를 브리핑 요청 대비 과하다고 막음). 이번 작업 지시와 이전 대화를 나눠 표시한다."""
    if not transcript_path:
        return ""
    try:
        from vmc_harness import sessions
        s = sessions.parse(transcript_path, agent)
    except Exception:
        return ""
    prior, start, during = _split_prompts(s.human_prompts, since)
    if not start and not during:
        return ""
    out = "[이번 작업을 시작한 사람 지시]\n" + start
    if during:
        out += "\n\n[작업 중 사람 지시]\n" + "\n---\n".join(during)
    out = out[-max_chars:]
    plan = _approved_plan(s, start, since, plan_max_chars, short_max_chars)
    if plan:  # 상한은 따로(overdesign_plan_max_chars) — 사람 지시 상한에 잘려 나가지 않게
        out = "[사람이 승인한 계획 — 직전 어시스턴트 제안]\n" + plan + "\n\n" + out
    if prior and prior_max_chars:
        out = "[이전 대화 — 맥락만, 이번 작업의 요청 아님]\n" + "\n---\n".join(prior)[-prior_max_chars:] + "\n\n" + out
    return out


def _declare_text(task):
    d = task.get("declare") or {}
    intent = (d.get("intent") or "").strip()
    if not intent:
        return ""
    end = (d.get("end") or "").strip()
    return "[착수 선언]\n" + intent + (" (" + end + ")" if end else "")


def _convention_tail(name):
    """'261114_홍길동_인터뷰_transcript.md' → '_transcript.md'. 이름에 구분자(_ -)가 없으면 None."""
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    m = re.search(r"[_\-][^_\-]+$", stem)
    if not m:
        return None
    return m.group(0)[1:] + (("." + ext) if ext else "")


def convention_files(root, task, files):
    """기존 관례를 따른 새 파일 — 같은 폴더에 이름 끝(마지막 _/- 뒤 + 확장자)이 같은 파일이 착수 시점에
    task.overdesign_convention_min개 이상 있었다. 반복 산출물(회의록·전사 등)을 '요청에 없는 것'으로 막지 않는다."""
    base = (task.get("base") or {}).get("head")
    existed = {_nfc(e) for e in _base_files(root, base)}
    need = config.get("task.overdesign_convention_min", root)
    out = set()
    for f in files or []:
        f = _nfc(scope.norm(f))
        if f in existed:
            continue
        p = PurePosixPath(f)
        tail = _convention_tail(p.name)
        if not tail:
            continue
        parent = str(p.parent)
        peers = [e for e in existed if str(PurePosixPath(e).parent) == parent
                 and (e.endswith("_" + tail) or e.endswith("-" + tail))]
        if len(peers) >= need:
            out.add(f)
    return out


def run_llm(root, task_id, transcript_path=None, agent=None):
    task = st.by_id(root, task_id)
    if not task:
        return None
    files = scope.touched(root, task)
    base = (task.get("base") or {}).get("head") or "HEAD"
    max_diff = config.get("task.overdesign_diff_max_chars", root)
    max_prompt = config.get("task.overdesign_prompt_max_chars", root)
    diff = ""
    if files:
        rc, out, _ = gitutil.git(root, "diff", base, "--", *files)
        diff = out if rc == 0 else ""
        for f in files:  # 미추적 새 파일 본문
            if len(diff) >= max_diff:
                break
            if f not in diff:
                body = _read(root, f)
                if body is not None:
                    diff += "\n+++ %s (새 파일)\n%s" % (f, body)
    diff = diff[:max_diff]
    prompts = _prompts_text(transcript_path, agent, max_prompt, since=task.get("started_at"),
                            prior_max_chars=config.get("task.overdesign_prior_prompt_max_chars", root),
                            plan_max_chars=config.get("task.overdesign_plan_max_chars", root),
                            short_max_chars=config.get("task.overdesign_short_ack_max_chars", root))
    declared = _declare_text(task)
    prompts = "\n\n".join(x for x in (declared, prompts) if x)
    rec = {"status": "done", "signals": [], "tokens": 0, "files": files}
    if diff.strip():
        res = None
        try:
            from vmc_harness.core.llm import claude_p
            from vmc_harness.task.pm import parse_json_obj
            res = claude_p("[사람 지시문]\n%s\n\n[diff]\n%s" % (prompts, diff), model=config.get("pm.model", root),
                           system=OD_SYSTEM, timeout=config.get("task.overdesign_llm_timeout_s", root))
        except Exception:
            res = None
        if res:
            rec["tokens"] = res.get("tokens", 0)
            obj = parse_json_obj(res.get("text")) or {}
            for s in obj.get("signals") or []:
                if isinstance(s, dict) and str(s.get("what") or "").strip():
                    rec["signals"].append({"kind": "unrequested_option", "path": s.get("path"),
                                           "name": str(s["what"])[:120]})
            rec["signals"] = own_signals(rec["signals"], files)
            rec["signals"], used = existing_usage(root, rec["signals"], files)
            rec["existing_usage"] = [x.get("name") for x in used]
            rec["signals"], had = existed_at_base(root, task, rec["signals"])
            rec["existed"] = [x.get("name") for x in had]
            conv = convention_files(root, task, files)
            rec["signals"] = [x for x in rec["signals"] if not x.get("path") or _nfc(scope.norm(x["path"])) not in conv]
            rec["convention"] = sorted(conv)
        try:
            from vmc_harness.core import events
            events.log(root, "overdesign_llm", task_id=task_id, called=bool(res), tokens=int(rec["tokens"] or 0),
                       sec=round(float((res or {}).get("sec") or 0), 2), signals=len(rec["signals"]))
        except Exception:
            pass
    write_json(od_path(root, task_id), rec)
    st.add_overhead(root, task_id, tokens=rec["tokens"])
    return rec


UNUSED_CLAIM = re.compile(r"쓰이지 않|안 쓰이|사용되지 않|사용하지 않|의미 없는|불필요한|unused", re.I)
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{2,}")


def _codeish(tok):
    """코드 식별자로 보이는 낱말(cs-top·is_hero·isHero) — 'div'·'flag' 같은 일반 낱말은 사용처를 따질 대상이 아니다."""
    return "-" in tok or "_" in tok or any(c.isupper() for c in tok[1:]) or any(c.isdigit() for c in tok)


def _used_outside(root, name, exclude):
    """이름이 이 작업이 쓴 파일(exclude) 밖, 저장소의 다른 파일에 단어로 나오는가."""
    rc, out, _ = gitutil.git(root, "grep", "-l", "-w", "-I", "--untracked", "-F", "-e", name)
    if rc not in (0, 1):
        return False
    for f in out.splitlines():
        f = _nfc(f.strip())
        if f and f not in exclude and not f.startswith(scope.HARNESS_PREFIX):
            return True
    return False


def existing_usage(root, signals, files):
    """'쓰이지 않는 장치'라고 지목한 신호인데, 지목한 이름이 모두 저장소의 다른 파일에서 이미 쓰이면 기존 관례다 — 신호에서 뺀다.
    LLM은 diff만 보므로 같은 레포의 사용처(CSS 선택자·프레임워크가 읽는 플래그 등)를 모르고 '안 쓰인다'고 한다(2026-11-15 현장).
    돌려주는 값: (남길 신호, 뺀 신호)."""
    mine = {_nfc(scope.norm(f)) for f in files or []}
    keep, dropped = [], []
    for sgl in signals or []:
        what = str(sgl.get("name") or "")
        names = sorted({m.group(0) for m in IDENT.finditer(what) if _codeish(m.group(0))}) if UNUSED_CLAIM.search(what) else []
        if names and all(_used_outside(root, n, mine) for n in names):
            dropped.append(sgl)
        else:
            keep.append(sgl)
    return keep, dropped


HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", re.M)
FIELD_CLAIM = re.compile(r"필드|속성|\bfield|\bprop", re.I)
WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")


def _headings(text):
    return {h.strip() for h in HEADING.findall(text or "") if len(h.strip()) >= 2}


def _sibling_heading_count(root, base, path, heading):
    """착수 시점(base) 같은 폴더의 다른 파일 중 이 제목의 절이 있는 파일 수."""
    parent = str(PurePosixPath(path).parent)
    pat = r"^[[:space:]]{0,3}#{1,6}[[:space:]]+" + re.sub(r"([\\.^$|?*+()\[\]{}])", r"\\\1", heading) + r"[[:space:]#]*$"
    rc, out, _ = gitutil.git(root, "grep", "-l", "-E", "-e", pat, base, "--", (parent + "/") if parent not in (".", "") else ".")
    if rc not in (0, 1):
        return 0
    n = 0
    for line in out.splitlines():
        f = _nfc(line.split(":", 1)[-1].strip())
        if str(PurePosixPath(f).parent) == parent and f != _nfc(path):
            n += 1
    return n


def existed_at_base(root, task, signals):
    """착수 때 이미 있던 것을 지목한 신호를 뺀다. LLM은 diff만 보므로 형식·필드가 새로 생긴 줄 안다(2026-11-12·13 현장).
    ① 지목한 절(마크다운 제목)이 같은 폴더 기존 파일 task.overdesign_convention_min개 이상에 이미 있다 — 기존 형식을 따른 것.
    ② '필드·속성'이라고 지목한 이름(영문 식별자)이 모두 착수 시점 그 파일에 이미 있다 — 새 필드가 아니다.
    돌려주는 값: (남길 신호, 뺀 신호)."""
    base = (task.get("base") or {}).get("head")
    if not base:
        return list(signals or []), []
    need = config.get("task.overdesign_convention_min", root)
    keep, dropped = [], []
    for sgl in signals or []:
        what, path = str(sgl.get("name") or ""), sgl.get("path")
        hit = False
        if path:
            path = _nfc(scope.norm(path))
            old = _show(root, base, path)
            added = _headings(_read(root, path)) - _headings(old)
            hit = any(h in what and _sibling_heading_count(root, base, path, h) >= need for h in added)
            if not hit and old and FIELD_CLAIM.search(what):
                names = {w for w in WORD.findall(what) if w.lower() not in ("field", "fields", "prop", "props")}
                hit = bool(names) and all(re.search(r"\b%s\b" % re.escape(w), old) for w in names)
        (dropped if hit else keep).append(sgl)
    return keep, dropped


def shown(signals):
    """막을 때 보일 신호 — 무엇이 잡혔는지 말할 수 있는 것만. 작업 중 주입 금지 숫자(시간·퍼센트 등)가 든 지적은 숫자를
    가려 싣고, 그래도 실을 수 없거나 내용이 비면 뺀다. 이전에는 통째로 가린 '요청에 없는 옵션·기능 ' 빈 차단이 났다(2026-11-14 현장)."""
    from vmc_harness.core import inject
    out = []
    for sgl in signals or []:
        if not (str(sgl.get("name") or "").strip() or str(sgl.get("path") or "").strip()):
            continue
        if inject.message("gate_violation", describe([sgl])):
            out.append(sgl)
            continue
        masked = dict(sgl, name=re.sub(r"\d+(?:[.,:~\-]\d+)*", "○", str(sgl.get("name") or "")),
                      detail=re.sub(r"\d+(?:[.,:~\-]\d+)*", "○", str(sgl.get("detail") or "")) or None)
        if (masked["name"].strip() or str(sgl.get("path") or "").strip()) and inject.message("gate_violation", describe([masked])):
            out.append(masked)
    return out


def blocking(signals):
    """막는 신호 — NOTE_ONLY(새 파일·새 모듈)는 완료 보고에만."""
    return [s for s in signals or [] if s.get("kind") not in NOTE_ONLY]


def own_signals(signals, files):
    """이 작업이 쓴 파일(task.scope.touched)에 대한 신호만. LLM이 지시문에서 본 남의 파일, 결과가 늦게 읽히는 동안
    바뀐 귀속(다른 세션이 쓴 파일이 그때는 섞여 있던 것)으로 막지 않는다. 경로가 없는 신호는 남긴다."""
    mine = {_nfc(scope.norm(f)) for f in files or []}
    out = []
    for s in signals or []:
        p = s.get("path")
        if p:
            p = _nfc(scope.norm(p))
            if p not in mine and not any(f.endswith("/" + p) for f in mine):
                continue
        out.append(s)
    return out


def read_llm(root, task_id, files=None):
    """LLM 판정 결과. files(지금 이 작업이 쓴 파일)를 주면 그 파일에 대한 신호만 남긴다."""
    rec = read_json(od_path(root, task_id))
    if not rec or rec.get("status") != "done":
        return None
    if files is not None:
        rec = dict(rec, signals=own_signals(rec.get("signals"), files))
    return rec


def describe(signals):
    """"과설계 신호: …" — 무엇이 잡혔는지만."""
    parts = []
    for s in signals:
        label = LABEL.get(s["kind"], s["kind"])
        what = s.get("name") or ""
        where = s.get("path") or ""
        if s["kind"] == "near_duplicate_function" and s.get("detail"):
            parts.append("%s %s(%s) ≈ %s" % (label, what, where, s["detail"]))
        elif what and where:
            parts.append("%s %s(%s)" % (label, what, where))
        else:
            parts.append("%s %s" % (label, what or where))
    return "과설계 신호: " + " · ".join(parts)
