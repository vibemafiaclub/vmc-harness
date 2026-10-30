"""고정 판정 (spec 06) — 메타 게이트가 기계로 검사한다. B등급 자동 머지의 조건.

① 변경 **전에** 현재 동작 고정 테스트를 쓰고(별도 커밋) 변경 전후 모두 통과
② 바뀐 줄(새 쪽 실행 가능한 줄)이 전부 그 테스트에 덮임 — Python은 표준 라이브러리 settrace(`_cov_runner.py`),
   그 밖의 언어는 "줄마다 죽은 돌연변이가 있다"로 대신 증명(죽은 돌연변이 = 그 줄이 실행됐다)
③ 돌연변이 점검 — 바뀐 코드를 일부러 망가뜨리면(비교·산술·논리 연산자 뒤집기, 상수 바꾸기, 반환값 없애기, 조건 뒤집기)
   고정 테스트가 실제로 실패하는가. 죽인 비율 ≥ `loop.mutation_min_kill_ratio`
④ 바뀐 파일을 덮는 QA 시나리오가 있고 전부 PASS, 그중 회귀 없음
하나라도 측정 못 하면 통과로 치지 않는다(자동 머지는 안전 쪽).
"""

import ast
import copy
import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path

from vmc_harness.core import config
from vmc_harness.core.gitutil import git
from vmc_harness.loop import detect, gitops, procx

COV_RUNNER = Path(__file__).resolve().parent / "_cov_runner.py"


# ---- 테스트 실행 명령 ----

def _facts(root):
    try:
        from vmc_harness.bootstrap import facts
        return facts.load(root) or {}
    except (ImportError, AttributeError):
        return {}
    except Exception:
        return {}


def _fact_cmd(facts, name):
    v = facts.get("%s_command" % name) or (facts.get("commands") or {}).get(name)
    if isinstance(v, dict):
        v = v.get("command")
    return v


def project_python(root):
    f = _facts(root)
    if f.get("python"):
        return f["python"]
    for venv in (".venv", "venv"):
        p = Path(root) / venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        if p.exists():
            return str(p)
    return sys.executable


def _split(cmd):
    return cmd if isinstance(cmd, list) else shlex.split(cmd, posix=(sys.platform != "win32"))


def _py_has(py, module, cwd):
    from vmc_harness.core.proc import run
    rc, _, _ = run([py, "-c", "import %s" % module], cwd=cwd, timeout=60)
    return rc == 0


def test_invocation(root, wt, test_files):
    """(argv, (module, module_args) | None) — 고정 테스트만 돌리는 명령. 못 정하면 (None, None)."""
    py_tests = [f for f in test_files if f.endswith(".py")]
    other = [f for f in test_files if not f.endswith(".py")]
    if py_tests and not other:
        py = project_python(root)
        texts = [detect._read(wt, f) or "" for f in py_tests]
        pytest_style = any("import pytest" in t or not re.search(r"\bunittest\b", t) for t in texts)
        if pytest_style and _py_has(py, "pytest", wt):
            margs = ["-q", "-p", "no:cacheprovider"] + py_tests
            return [py, "-m", "pytest"] + margs, ("pytest", margs)
        return [py, "-m", "unittest"] + py_tests, ("unittest", py_tests)
    cmd = _fact_cmd(_facts(root), "test")
    if cmd and test_files:
        argv = _split(cmd)
        if Path(argv[0]).name.split(".")[0] in ("npm", "pnpm", "yarn") and "--" not in argv:
            argv.append("--")  # 패키지 매니저 스크립트에 파일 인자 넘기기
        return argv + list(test_files), None
    return None, None


def runner_hint(root, wt, lang="python"):
    """고정 테스트를 쓸 모델에게 주는 사실: 이 프로젝트에서 테스트가 도는 방식."""
    cmd = _fact_cmd(_facts(root), "test")
    if lang == "python":
        py = project_python(root)
        if _py_has(py, "pytest", wt):
            return "python -m pytest (pytest 있음)"
        return "python -m unittest (pytest 없음 — unittest만 쓸 수 있다)"
    return cmd or "테스트 명령 미확인"


def run_tests(root, wt, argv, env=None, deadline=None):
    """(rc, interrupted)."""
    e = dict(os.environ)
    e["VMC_HARNESS_NESTED"] = "1"
    if env:
        e.update(env)
    rc, _, _, intr = procx.run(argv, cwd=str(wt), timeout=config.get("loop.test_timeout_s", root), env=e,
                               root=root, deadline=deadline)
    return rc, intr


# ---- ② 덮임 ----

def executable_lines(text):
    import dis
    try:
        code = compile(text, "<m>", "exec")
    except SyntaxError:
        return set()
    lines = set()
    stack = [code]
    while stack:
        c = stack.pop()
        for _, ln in dis.findlinestarts(c):
            if ln:
                lines.add(ln)
        for k in c.co_consts:
            if hasattr(k, "co_code"):
                stack.append(k)
    # 독스트링·def 줄은 빼지 않는다(실행된다). 주석·빈 줄은 애초에 없다
    return lines


def py_coverage(root, wt, module_call, targets, deadline=None):
    """{rel: set(실행된 줄)} 또는 None(측정 실패)."""
    py = project_python(root)
    fd, out = tempfile.mkstemp(prefix="vmh-cov-", suffix=".json")
    os.close(fd)
    try:
        abs_targets = os.pathsep.join(str((Path(wt) / t).resolve()) for t in targets)
        argv = [py, str(COV_RUNNER), out, abs_targets, "--", module_call[0]] + list(module_call[1])
        rc, intr = run_tests(root, wt, argv, deadline=deadline)
        if intr or rc != 0:
            return None
        with open(out, "r", encoding="utf-8") as f:
            data = json.load(f)
        res = {}
        wtr = Path(wt).resolve()
        for absf, lines in (data.get("files") or {}).items():
            try:
                rel = Path(absf).resolve().relative_to(wtr).as_posix()
            except ValueError:
                continue
            res[rel] = set(lines)
        return res
    except (OSError, ValueError):
        return None
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


# ---- ③ 돌연변이 ----

_CMP_SWAP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt, ast.Gt: ast.LtE,
             ast.LtE: ast.Gt, ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is}
_BIN_SWAP = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult,
             ast.FloorDiv: ast.Mult, ast.Mod: ast.Mult}


def _py_mutation_sites(tree, lines):
    """[(walk 순번, 연산자 이름, 줄)]"""
    sites = []
    docstrings = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.body \
                and isinstance(n.body[0], ast.Expr) and isinstance(getattr(n.body[0], "value", None), ast.Constant):
            docstrings.add(id(n.body[0].value))
    for i, n in enumerate(ast.walk(tree)):
        ln = getattr(n, "lineno", None)
        if ln not in lines:
            continue
        if isinstance(n, ast.Compare) and type(n.ops[0]) in _CMP_SWAP:
            sites.append((i, "compare", ln))
        elif isinstance(n, ast.BinOp) and type(n.op) in _BIN_SWAP:
            sites.append((i, "binop", ln))
        elif isinstance(n, ast.BoolOp):
            sites.append((i, "boolop", ln))
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            sites.append((i, "not", ln))
        elif isinstance(n, ast.Constant) and id(n) not in docstrings and isinstance(n.value, (bool, int, float, str)):
            sites.append((i, "const", ln))
        elif isinstance(n, ast.Return) and n.value is not None:
            sites.append((i, "return", ln))
        elif isinstance(n, (ast.If, ast.While)):
            sites.append((i, "cond", ln))
    return sites


def _apply_py(tree, idx, kind):
    t = copy.deepcopy(tree)
    target = None
    for i, n in enumerate(ast.walk(t)):
        if i == idx:
            target = n
            break
    if target is None:
        return None
    if kind == "compare":
        target.ops[0] = _CMP_SWAP[type(target.ops[0])]()
    elif kind == "binop":
        target.op = _BIN_SWAP[type(target.op)]()
    elif kind == "boolop":
        target.op = ast.Or() if isinstance(target.op, ast.And) else ast.And()
    elif kind == "not":
        # not x → x : 부모에서 바꾸기 어려워 이중 부정으로 같은 효과
        target.operand = ast.UnaryOp(op=ast.Not(), operand=target.operand)
    elif kind == "const":
        v = target.value
        if isinstance(v, bool):
            target.value = not v
        elif isinstance(v, (int, float)):
            target.value = v + 1
        else:
            target.value = (v + "_mut") if v else "mut"
    elif kind == "return":
        target.value = ast.Constant(value=None)
    elif kind == "cond":
        target.test = ast.UnaryOp(op=ast.Not(), operand=target.test)
    ast.fix_missing_locations(t)
    try:
        return ast.unparse(t)
    except Exception:
        return None


_TEXT_OPS = [(r"===", "!=="), (r"!==", "==="), (r"(?<![=!<>])==(?!=)", "!="), (r"!=(?!=)", "=="),
             (r"<=", ">"), (r">=", "<"), (r"(?<![<=>-])<(?![<=])", ">="), (r"(?<![<=>-])>(?![>=])", "<="),
             (r"&&", "||"), (r"\|\|", "&&"), (r"\btrue\b", "false"), (r"\bfalse\b", "true"),
             (r"(?<![+\-])\+(?![+=])", "-"), (r"(?<![+\-])-(?![-=>])", "+"),
             (r"\breturn\s+([^;\n]+)", "return undefined")]


def _spread(sites, limit):
    """줄마다 고르게(라운드로빈) limit개까지."""
    by_line = {}
    for s in sites:
        by_line.setdefault(s[-1], []).append(s)
    out = []
    while len(out) < limit and any(by_line.values()):
        for ln in sorted(by_line):
            if by_line[ln] and len(out) < limit:
                out.append(by_line[ln].pop(0))
    return out


def mutants_for(text, rel, lines, limit, per_line=False):
    """[(줄, 변이된 파일 전체 텍스트, 설명)]. per_line=True면 줄마다 최소 1개를 먼저 보장."""
    out = []
    if rel.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return []
        sites = _py_mutation_sites(tree, lines)
        chosen = _spread(sites, limit if not per_line else max(limit, len({s[-1] for s in sites})))
        for idx, kind, ln in chosen:
            m = _apply_py(tree, idx, kind)
            if m and m != text:
                out.append((ln, m, kind))
        return out
    src_lines = text.splitlines(True)
    sites = []
    for ln in sorted(lines):
        if ln < 1 or ln > len(src_lines):
            continue
        line = src_lines[ln - 1]
        if re.match(r"\s*(//|/\*|\*|import\b|export\s+\*|$)", line):
            continue
        for pat, rep in _TEXT_OPS:
            if re.search(pat, line):
                sites.append((pat, rep, ln))
    chosen = _spread(sites, limit if not per_line else max(limit, len({s[-1] for s in sites})))
    for pat, rep, ln in chosen:
        new_line = re.sub(pat, rep, src_lines[ln - 1], count=1)
        m = "".join(src_lines[:ln - 1] + [new_line] + src_lines[ln:])
        if m != text:
            out.append((ln, m, pat))
    return out


def mutation_check(root, wt, argv, changed, deadline=None, per_line=False):
    """{"total","killed","ratio","lines_killed":{rel:[줄]}, "interrupted"} — changed={rel: set(새 쪽 줄)}."""
    limit = config.get("loop.mutants_max", root)
    total, killed, lines_killed = 0, 0, {}
    for rel, lines in changed.items():
        p = Path(wt) / rel
        if not p.is_file() or not lines:
            continue
        original = p.read_text(encoding="utf-8")
        try:
            for ln, mutated, _ in mutants_for(original, rel, lines, limit, per_line=per_line):
                p.write_text(mutated, encoding="utf-8")
                rc, intr = run_tests(root, wt, argv, deadline=deadline)
                if intr:
                    return {"total": total, "killed": killed, "ratio": 0.0, "lines_killed": lines_killed,
                            "interrupted": intr}
                total += 1
                if rc != 0:
                    killed += 1
                    lines_killed.setdefault(rel, set()).add(ln)
        finally:
            p.write_text(original, encoding="utf-8")
    return {"total": total, "killed": killed, "ratio": (killed / total) if total else 0.0,
            "lines_killed": lines_killed, "interrupted": None}


# ---- 전체 판정 ----

def code_changes(wt, a, b):
    """새 쪽 바뀐 줄 {rel: set} (테스트 파일 제외, 지워진 파일 제외)."""
    out = {}
    for rel, d in gitops.changed_lines(wt, a, b).items():
        if detect.is_test_path(rel) or d.get("deleted"):
            continue
        out[rel] = set(d["added"])
    return out


def check(root, wt, base, pin_commit, change_commit, deadline=None):
    """고정 판정 ①~④. {"ok": bool, "1": bool, "2": bool, "3": bool, "4": bool, "detail": {...}}"""
    res = {"ok": False, "1": False, "2": False, "3": False, "4": False, "detail": {}}
    d = res["detail"]
    pin_files = gitops.changed_files(wt, base, pin_commit)
    if not pin_files or not all(detect.is_test_path(f) for f in pin_files):
        d["1"] = "고정 테스트 커밋이 없거나 테스트 밖 파일을 건드림"
        return res
    later = gitops.changed_files(wt, pin_commit, change_commit)
    if any(f in pin_files for f in later):
        d["1"] = "변경 커밋이 고정 테스트를 고침"
        return res
    argv, module_call = test_invocation(root, wt, pin_files)
    if not argv:
        d["1"] = "테스트 실행 명령을 정하지 못함"
        return res
    # ① 전: pin_commit에서 통과
    git(wt, "checkout", "-q", pin_commit)
    try:
        rc, intr = run_tests(root, wt, argv, deadline=deadline)
    finally:
        git(wt, "checkout", "-q", "-")
    if intr:
        d["interrupted"] = intr
        return res
    if rc != 0:
        d["1"] = "변경 전 고정 테스트 실패"
        return res
    git(wt, "checkout", "-q", change_commit)
    try:
        rc, intr = run_tests(root, wt, argv, deadline=deadline)
        if intr:
            d["interrupted"] = intr
            return res
        if rc != 0:
            d["1"] = "변경 후 고정 테스트 실패"
            return res
        res["1"] = True
        changed = code_changes(wt, pin_commit, change_commit)
        # 실행 가능한 줄만 대상으로
        targets = {}
        for rel, lines in changed.items():
            text = detect._read(wt, rel) or ""
            if rel.endswith(".py"):
                lines = lines & executable_lines(text)
            else:
                src = text.splitlines()
                lines = {ln for ln in lines if 0 < ln <= len(src) and src[ln - 1].strip()
                         and not re.match(r"\s*(//|/\*|\*|[}\])]+;?\s*$)", src[ln - 1])}
            if lines:
                targets[rel] = lines
        d["changed_lines"] = sum(len(v) for v in targets.values())
        if not targets:
            d["2"] = d["3"] = "바뀐 실행 줄 없음(측정 대상 없음)"
            return res
        # ② 덮임
        uncovered = {}
        py_targets = {k: v for k, v in targets.items() if k.endswith(".py")}
        if py_targets:
            if not module_call:
                d["2"] = "Python 덮임 측정 불가(테스트 실행 형태)"
                return res
            cov = py_coverage(root, wt, module_call, list(py_targets), deadline=deadline)
            if cov is None:
                d["2"] = "덮임 측정 실패"
                return res
            for rel, lines in py_targets.items():
                miss = lines - cov.get(rel, set())
                if miss:
                    uncovered[rel] = sorted(miss)
        other = {k: v for k, v in targets.items() if not k.endswith(".py")}
        # ③ 돌연변이 (Python 아닌 줄은 줄마다 하나씩 — ② 대체 증명 겸용)
        mut = mutation_check(root, wt, argv, targets, deadline=deadline, per_line=bool(other))
        if mut.get("interrupted"):
            d["interrupted"] = mut["interrupted"]
            return res
        for rel, lines in other.items():
            miss = lines - mut["lines_killed"].get(rel, set())
            if miss:
                uncovered[rel] = sorted(miss)
        d["uncovered"] = uncovered
        res["2"] = not uncovered
        d["mutation"] = {"total": mut["total"], "killed": mut["killed"]}
        res["3"] = mut["total"] > 0 and mut["ratio"] >= config.get("loop.mutation_min_kill_ratio", root)
    finally:
        git(wt, "checkout", "-q", "-")
    # ④ QA 시나리오
    res["4"], d["4"] = qa_check(root, list(changed.keys()))
    res["ok"] = all(res[k] for k in ("1", "2", "3", "4"))
    return res


def qa_check(root, rel_paths):
    try:
        from vmc_harness.qa import results, scenarios
    except ImportError:
        return False, "QA 모듈 없음"
    ids = scenarios.covering(root, rel_paths)
    if not ids:
        return False, "해당 페이지 QA 시나리오 없음"
    reg = results.regressions(root, ids)
    if reg:
        return False, "QA 회귀: %s" % ", ".join(reg)
    if not results.all_pass(root, ids):
        return False, "QA 시나리오 미통과"
    return True, ids
