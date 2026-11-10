"""기계 사실 (spec 07 §부트스트랩, 01 P3).

결정적으로 읽은 값만. **각 사실은 읽어 온 방법(command)과 함께 저장**하고 `recheck()`로 다시 읽어 값이 바뀌면
사실과 규칙 블록(machine_fact)을 함께 바꾼다.

저장: `.harness/cache/facts.json` — {"generated_at", "facts": {키: {"value","command","reader","read_at"}}}
규칙 블록에 갈 환경 사실: `.harness/rules.json`에 basis.type=machine_fact로(스키마는 gates 영역과 공유)
"""

import json
import os
import re
import sys
from pathlib import Path, PurePosixPath

from vmc_harness.bootstrap import util
from vmc_harness.core import config, paths
from vmc_harness.core.clock import now, now_iso, parse_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.proc import run

MANIFESTS = ("package.json", "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "go.mod", "pom.xml",
             "build.gradle", "Cargo.toml", "Gemfile", "composer.json")
NON_SOURCE_TOP = {"docs", "doc", "documentation", ".github", ".gitlab", "assets", "images", "examples", "samples"}
RULES_FILE = "rules.json"


def facts_path(root):
    return paths.cache_dir(root) / "facts.json"


def rules_path(root):
    return paths.harness_dir(root) / RULES_FILE


# ---------------------------------------------------------------- 읽기 장치(reader) — 다시 실행할 수 있는 형태

def _reader_json_key(root, path, key):
    try:
        data = json.loads(util.read_text(Path(root) / path))
    except ValueError:
        return None
    for k in key.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(k)
    return data


def _reader_file_regex(root, path, pattern):
    text = util.read_text(Path(root) / path)
    return sorted(set(m.group(0) for m in re.finditer(pattern, text, re.I | re.M)))


def _reader_exists(root, path):
    return (Path(root) / path).exists()


READERS = {"json_key": _reader_json_key, "file_regex": _reader_file_regex, "exists": _reader_exists}


def run_reader(root, reader):
    fn = READERS.get(reader.get("kind"))
    if not fn:
        return None
    args = {k: v for k, v in reader.items() if k != "kind"}
    return fn(root, **args)


def _fact(value, command, reader=None):
    return {"value": value, "command": command, "reader": reader, "read_at": now_iso()}


# ---------------------------------------------------------------- 소스 루트

def detect_source_roots(root):
    """(목록, 읽은 방법). 루트에 manifest가 있으면 '.', 아니면 manifest가 있는 1단 폴더 + 소스 파일이 있는 1단 폴더."""
    root = Path(root)
    if any((root / m).is_file() for m in MANIFESTS):
        return ["."], "루트에 manifest(%s) 있음" % ", ".join(m for m in MANIFESTS if (root / m).is_file())
    roots, why = [], []
    # 이름(코드포인트)으로 정렬 — Path 비교는 Windows에서 대소문자를 무시해 OS마다 순서가 달라진다
    for d in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name):
        if d.name in util.IGNORE_DIRS or d.name.startswith(".") or d.name.lower() in NON_SOURCE_TOP:
            continue
        ms = [m for m in MANIFESTS if (d / m).is_file()]
        if ms:
            roots.append(d.name)
            why.append("%s/%s" % (d.name, ms[0]))
            continue
        files, _ = util.walk_files(d, util.SOURCE_EXT, limit=1)
        if files:
            roots.append(d.name)
            why.append("%s/ 소스 파일" % d.name)
    if not roots:
        return ["."], "manifest·소스 폴더 없음 → 루트"
    return roots, "1단 폴더 탐색: " + ", ".join(why)


# ---------------------------------------------------------------- 명령(빌드·테스트·린트)

_KIND_RX = [
    ("test", re.compile(r"(?i)^(test|tests|unit|e2e|spec|smoke|check:test)")),
    ("lint", re.compile(r"(?i)(lint|eslint|ruff|flake8|pylint|format|prettier|stylelint)")),
    ("typecheck", re.compile(r"(?i)(typecheck|type-check|tsc|mypy|pyright)")),
    ("build", re.compile(r"(?i)^(build|prebuild|compile|bundle)")),
    ("dev", re.compile(r"(?i)^(dev|start|serve)")),
]


def _kind_of(name):
    for k, rx in _KIND_RX:
        if rx.search(name):
            return k
    return "other"


def _toml_sections(text):
    return re.findall(r"^\s*\[([^\]]+)\]", text, re.M)


def detect_commands(root, source_roots):
    """[{dir, kind, name, run, source, command}]"""
    root = Path(root)
    out = []
    dirs = sorted(set(["."] + list(source_roots)))
    for d in dirs:
        base = root / d
        pj = base / "package.json"
        if pj.is_file():
            rel = util.rel(root, pj)
            scripts = _reader_json_key(root, rel, "scripts") or {}
            for name, cmd in scripts.items():
                out.append({"dir": d, "kind": _kind_of(name), "name": name, "run": "npm run %s" % name,
                            "script": cmd, "source": rel,
                            "command": "read %s#scripts.%s" % (rel, name)})
        pp = base / "pyproject.toml"
        if pp.is_file():
            rel = util.rel(root, pp)
            text = util.read_text(pp)
            secs = _toml_sections(text)
            if "tool.pytest.ini_options" in secs:
                out.append({"dir": d, "kind": "test", "name": "pytest", "run": "python -m pytest", "source": rel,
                            "command": "read %s#[tool.pytest.ini_options]" % rel})
            for tool, kind in (("ruff", "lint"), ("black", "lint"), ("flake8", "lint"), ("mypy", "typecheck"),
                               ("pyright", "typecheck"), ("isort", "lint")):
                if any(s == "tool." + tool or s.startswith("tool.%s." % tool) for s in secs):
                    out.append({"dir": d, "kind": kind, "name": tool, "run": "python -m %s" % tool, "source": rel,
                                "command": "read %s#[tool.%s]" % (rel, tool)})
        for ini in ("pytest.ini", "tox.ini", "setup.cfg"):
            p = base / ini
            if p.is_file() and re.search(r"^\[(pytest|tool:pytest)\]", util.read_text(p), re.M):
                rel = util.rel(root, p)
                if not any(c["dir"] == d and c["name"] == "pytest" for c in out):
                    out.append({"dir": d, "kind": "test", "name": "pytest", "run": "python -m pytest", "source": rel,
                                "command": "read %s#[pytest]" % rel})
        mk = base / "Makefile"
        if mk.is_file():
            rel = util.rel(root, mk)
            for t in re.findall(r"^([A-Za-z][\w\-]*)\s*:(?!=)", util.read_text(mk), re.M):
                out.append({"dir": d, "kind": _kind_of(t), "name": t, "run": "make %s" % t, "source": rel,
                            "command": "read %s#target %s" % (rel, t)})
        sd = base / "scripts"
        if sd.is_dir():
            for f in sorted(sd.iterdir(), key=lambda p: p.name):
                if f.is_file() and f.suffix.lower() in (".sh", ".ps1", ".cmd", ".bat", ".py", ".mjs", ".js"):
                    rel = util.rel(root, f)
                    out.append({"dir": d, "kind": _kind_of(f.stem), "name": f.name, "run": rel, "source": rel,
                                "command": "ls %s" % util.rel(root, sd)})
        # 테스트 폴더는 있는데 설정이 없는 Python 프로젝트
        if not any(c["dir"] == d and c["name"] == "pytest" for c in out):
            tests = base / "tests"
            if tests.is_dir() and util.walk_files(tests, {".py"}, limit=1)[0]:
                out.append({"dir": d, "kind": "test", "name": "pytest", "run": "python -m pytest",
                            "source": util.rel(root, tests), "command": "ls %s (test_*.py)" % util.rel(root, tests)})
    return out


# ---------------------------------------------------------------- 테스트 기준선 (새 실패 0건 방식)

def find_python(root, d):
    """그 폴더의 가상환경 파이썬. 없으면 하네스를 돌리는 파이썬."""
    base = Path(root) / d
    for v in (".venv", "venv", "env"):
        for rel in ("bin/python", "Scripts/python.exe"):
            p = base / v / rel
            if p.is_file():
                return str(p), "%s/%s" % (util.rel(root, base / v), rel)
    for v in (".venv", "venv"):
        for rel in ("bin/python", "Scripts/python.exe"):
            p = Path(root) / v / rel
            if p.is_file():
                return str(p), "%s/%s" % (v, rel)
    return sys.executable, "하네스 실행 파이썬(가상환경 없음)"


_PYTEST_ERR_FILE = re.compile(r"^ERROR (\S+)(?: - (.*))?$", re.M)
_PYTEST_FAILED = re.compile(r"^FAILED (\S+)(?: - (.*))?$", re.M)
_PYTEST_COLLECT_BLOCK = re.compile(r"_{3,} ERROR collecting (\S+) _{3,}\n(.*?)(?=\n_{3,} |\n={3,} )", re.S)
_PYTEST_SUMMARY = re.compile(r"(\d+) (passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)")


def _collect_reasons(out):
    reasons = {}
    for m in _PYTEST_COLLECT_BLOCK.finditer(out):
        lines = [l for l in m.group(2).splitlines() if l.startswith("E ")]
        reasons[m.group(1)] = lines[-1][1:].strip()[:300] if lines else ""
    return reasons


def _summary_counts(out):
    tail = "\n".join(out.strip().splitlines()[-3:])
    counts = {}
    for n, k in _PYTEST_SUMMARY.findall(tail):
        k = "errors" if k.startswith("error") else ("warnings" if k.startswith("warning") else k)
        counts[k] = int(n)
    return counts


def pytest_baseline(root, d, run_full=True):
    root = Path(root)
    py, py_src = find_python(root, d)
    cwd = str(root / d)
    rc, _, _ = run([py, "-c", "import pytest"], cwd=cwd, timeout=config.get("bootstrap.command_timeout_s", root))
    res = {"runner": "pytest", "dir": d, "python": py_src, "status": None}
    if rc != 0:
        res["status"] = "not_run"
        res["reason"] = "pytest 없음(%s)" % py_src
        return res
    base = [py, "-m", "pytest", "-p", "no:cacheprovider"]
    collect_cmd = base + ["--co", "-q"]
    rc, out, err = run(collect_cmd, cwd=cwd, timeout=config.get("bootstrap.collect_timeout_s", root))
    res["collect_command"] = " ".join(["python"] + collect_cmd[1:])
    if rc == -9:
        res["status"] = "timeout"
        res["reason"] = "수집 시간 초과(%ss)" % config.get("bootstrap.collect_timeout_s", root)
        return res
    reasons = _collect_reasons(out)
    errs = sorted(set(p for p, _ in _PYTEST_ERR_FILE.findall(out)) | set(reasons))
    m = re.search(r"(\d+) tests? collected", out)
    res["collected"] = int(m.group(1)) if m else None
    res["collection_errors"] = [{"path": p, "reason": reasons.get(p, "")} for p in errs]
    if not run_full:
        res["status"] = "collected"
        return res
    full_cmd = base + ["-q", "--continue-on-collection-errors", "-rfE", "--tb=no", "--no-header"]
    res["command"] = " ".join(["python"] + full_cmd[1:])
    timeout = config.get("bootstrap.test_timeout_s", root)
    rc, out, err = run(full_cmd, cwd=cwd, timeout=timeout)
    if rc == -9:
        res["status"] = "timeout"
        res["reason"] = "실행 시간 초과(%ss) — 실패 목록 없음" % timeout
        return res
    res["failed"] = sorted(set(p for p, _ in _PYTEST_FAILED.findall(out)))
    res["errors"] = sorted(set(p for p, _ in _PYTEST_ERR_FILE.findall(out)) - set(errs))
    res["counts"] = _summary_counts(out)
    res["exit_code"] = rc
    res["status"] = "pass" if rc == 0 else "fail"
    return res


def npm_baseline(root, d, script="test"):
    root = Path(root)
    base = root / d
    res = {"runner": "npm", "dir": d, "status": None, "command": "npm run %s" % script}
    if not (base / "node_modules").is_dir():
        res["status"] = "not_run"
        res["reason"] = "의존성 미설치(node_modules 없음)"
        return res
    import shutil
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        res["status"] = "not_run"
        res["reason"] = "npm 없음"
        return res
    timeout = config.get("bootstrap.test_timeout_s", root)
    rc, out, err = run([npm, "run", script], cwd=str(base), timeout=timeout)
    if rc == -9:
        res["status"] = "timeout"
        res["reason"] = "실행 시간 초과(%ss)" % timeout
        return res
    res["exit_code"] = rc
    res["status"] = "pass" if rc == 0 else "fail"
    res["tail"] = util.scrub_user("\n".join((out + err).strip().splitlines()[-5:]))[:600]
    return res


def test_baseline(root, commands, run_tests=True):
    """[기준선] — 소스 루트별. 실패 목록이 기준선(새 실패 0건 방식). 수집 오류도 기록."""
    out = []
    seen = set()
    for c in commands:
        if c["kind"] != "test" or (c["dir"], c["name"]) in seen:
            continue
        if c["name"] == "pytest":
            seen.add((c["dir"], c["name"]))
            out.append(pytest_baseline(root, c["dir"], run_full=run_tests))
        elif c["run"] == "npm run test":
            seen.add((c["dir"], c["name"]))
            out.append(npm_baseline(root, c["dir"]) if run_tests else
                       {"runner": "npm", "dir": c["dir"], "status": "not_run", "reason": "--no-tests"})
    for d in sorted(set(c["dir"] for c in commands if c["source"].endswith("package.json"))):
        if not any(c["dir"] == d and c["kind"] == "test" for c in commands):
            out.append({"runner": "npm", "dir": d, "status": "no_command", "reason": "package.json에 test 스크립트 없음"})
    return out


# ---------------------------------------------------------------- DB·SQL·환경

_DB_PATTERNS = [
    ("mssql", r"(pyodbc|pymssql|mssql|sql ?server|sqlcmd|tedious|mssql\+)"),
    ("postgres", r"(psycopg2?|asyncpg|postgres(ql)?|\"pg\")"),
    ("mysql", r"(pymysql|mysqlclient|mysql2?|mariadb)"),
    ("oracle", r"(cx_oracle|oracledb)"),
    ("sqlite", r"(sqlite3?)"),
]


def detect_db(root, source_roots):
    """DB 종류(의존성·설정에서). db 영역 db/facts.py가 있으면 호환성 수준 등을 그쪽에서."""
    root = Path(root)
    files = []
    for d in sorted(set(["."] + list(source_roots))):
        for m in ("pyproject.toml", "requirements.txt", "package.json", "setup.cfg", ".env.example", "docker-compose.yml"):
            p = root / d / m
            if p.is_file():
                files.append(util.rel(root, p))
    found = {}
    for f in files:
        for kind, pat in _DB_PATTERNS:
            hits = _reader_file_regex(root, f, pat)
            if hits:
                found.setdefault(kind, []).append({"path": f, "pattern": pat, "hits": hits[:5]})
    kinds = sorted(found, key=lambda k: -sum(len(x["hits"]) for x in found[k]))
    value = {"kind": kinds[0] if kinds else None, "all": kinds, "evidence": found}
    st, extra = util.optional_call([("vmc_harness.db.facts", "load")], str(root))
    if st == "ok" and isinstance(extra, dict) and extra.get("facts"):
        # db-credential로 읽어 둔 값(호환성 수준 등 — 읽은 SQL은 db 영역이 함께 남긴다)
        value["db_facts"] = {f["key"]: {"value": f.get("value"), "command": f.get("command")}
                             for f in extra["facts"] if f.get("key")}
        value["kind"] = value["kind"] or extra.get("db_kind")
        lvl = (value["db_facts"].get("compatibility_level") or {}).get("value")
        if lvl is not None:
            st2, feats = util.optional_call([("vmc_harness.db.facts", "unavailable_features")], lvl)
            value["unavailable_features"] = feats if st2 == "ok" else []
    value["db_facts_status"] = st if (st != "ok" or value.get("db_facts")) else "no_credential"
    cmd = "; ".join("grep -i '%s' %s" % (x["pattern"], x["path"]) for k in kinds[:1] for x in found[k][:2]) or \
        "grep DB 드라이버 패턴 in %s" % ", ".join(files)
    return value, cmd


_SQL_PATCH_REF = re.compile(r"\b(SQL|sql|db|database)[/\\]+(?:(Patch|Patches|patch|patches)|(migrations?|Migrations?))\b")
_SQL_ROOT_JOIN = re.compile(r"(?i)sql_?(root|dir)")
_SQL_JOIN_PATCH = re.compile(r"""(?i)sql_?(?:root|dir)\s*/\s*["'](patch(?:es)?|migrations?)["']""")


def detect_sql_patch_dir(root):
    """SQL 패치 폴더 관례(`db.patch_dir` 후보). .sql 파일이 모인 폴더 + 코드가 가리키는 폴더."""
    root = Path(root)
    files, _ = util.walk_files(root, {".sql"}, limit=config.get("bootstrap.max_files", root))
    counts = {}
    for f in files:
        r = util.rel(root, f)
        parts = PurePosixPath(r).parts
        top = parts[0] if len(parts) > 1 else "."
        counts[top] = counts.get(top, 0) + 1
    referenced = {}
    code, _ = util.walk_files(root, {".py", ".sh", ".ps1", ".mjs", ".js", ".ts"}, limit=config.get("bootstrap.max_files", root))
    for f in code:
        text = util.read_text(f, limit=200000)
        if "sql" not in text.lower():
            continue
        hits = [(m.group(1) or "SQL") + "/" + (m.group(2) or m.group(3)) for m in _SQL_PATCH_REF.finditer(text)]
        if _SQL_ROOT_JOIN.search(text):
            hits += ["SQL/" + m.group(1) for m in _SQL_JOIN_PATCH.finditer(text)]
        for h in hits:
            r = referenced.setdefault(h.lower(), {"variants": {}, "by": set()})
            r["variants"][h] = r["variants"].get(h, 0) + 1
            r["by"].add(util.rel(root, f))
    cands = [{"dir": d, "sql_files": n} for d, n in sorted(counts.items(), key=lambda kv: -kv[1])]
    # 코드가 "패치 폴더"라고 가리키는 곳이 있으면 그것이 관례(없는 폴더여도 — 배포 도구가 거기서 읽는다)
    refs = []
    for r in referenced.values():  # 대소문자 표기가 갈리면 가장 많이 쓴 표기
        name = sorted(r["variants"].items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        refs.append({"dir": name, "files": len(r["by"]), "examples": sorted(r["by"])[:3]})
    refs.sort(key=lambda x: -x["files"])
    # SQL 파일이 모인 최상위 폴더를 기본으로(그 아래 Table·SP·Patch를 함께 담는다). 코드가 가리키는 패치 폴더가
    # 그 밖에 있을 때만 그것을 쓴다
    top = cands[0]["dir"] if cands else None
    if top and top != "." and (not refs or refs[0]["dir"].lower().startswith(top.lower() + "/")):
        suggested = top
    else:
        suggested = refs[0]["dir"] if refs else top
    value = {"candidates": cands, "referenced": refs,
             "referenced_missing": [x["dir"] for x in refs if not (root / x["dir"]).exists()],
             "suggested": suggested}
    return value, "find . -name '*.sql' | 1단 폴더별 개수; grep -E 'SQL[/\\\\](Patch|migrations)' 코드"


def detect_environment(root):
    root = Path(root)
    env = {}
    cmds = []
    win = sorted(util.rel(root, p) for p in list(root.glob("*.cmd")) + list(root.glob("*.bat")) + list(root.glob("*.ps1")))
    if win:
        env["dev_windows"] = win[:5]
        cmds.append("ls *.cmd *.bat *.ps1")
    linux = []
    for p in list(root.glob("scripts/*.sh")) + list(root.glob("deploy/**/*.sh")) + list(root.glob("**/Dockerfile*"))[:20]:
        t = util.read_text(p, limit=20000)
        m = re.search(r"(?i)\b(ubuntu|debian|centos|rhel|alpine|amazonlinux)\b", t)
        if m:
            linux.append({"path": util.rel(root, p), "os": m.group(1).lower()})
    if linux:
        env["prod_linux"] = sorted({x["os"] for x in linux})
        env["prod_linux_evidence"] = [x["path"] for x in linux[:5]]
        cmds.append("grep -i 'ubuntu|debian|alpine…' scripts/*.sh deploy/**/*.sh Dockerfile*")
    ga = root / ".gitattributes"
    if ga.is_file():
        rules = [l.strip() for l in util.read_text(ga).splitlines() if "eol=" in l]
        if rules:
            env["eol_rules"] = rules
            cmds.append("grep eol= .gitattributes")
    return env, "; ".join(cmds) or "ls *.cmd; grep Dockerfile"


def detect_agents_md(root):
    root = Path(root)
    out = {}
    for name in ("AGENTS.md", "CLAUDE.md", ".cursorrules"):
        p = root / name
        if p.is_file():
            t = util.read_text(p)
            out[name] = {"exists": True, "bytes": len(t.encode("utf-8")), "has_harness_block": "vmc-harness:begin" in t}
        else:
            out[name] = {"exists": False}
    return out, "ls AGENTS.md CLAUDE.md .cursorrules"


# ---------------------------------------------------------------- 동작·검증물 분류 glob (gates 완료 게이트·변조 금지가 읽는다)

def detect_behavior_globs(root, source_roots, commands, sql_patch):
    """{'page','api','db'} — 이 프로젝트 구조에서 결정적으로. 못 찾은 칸은 빼서 gates 기본값을 쓰게 한다."""
    root = Path(root)
    page, api, db = [], [], []
    for d in source_roots:
        base = root / d
        pre = "" if d == "." else d + "/"
        pj = base / "package.json"
        # 모노레포(apps/<앱>/package.json): 소스 루트 바로 아래 앱 폴더도 본다. 못 찾으면 page가 기본값(레포 전체 *.html·templates/**)으로
        # 떨어져 문서용 HTML·메시지 템플릿 JSON까지 페이지 변경이 된다(현장 결함 — 완료 게이트 오탐)
        apps = [(base, pre)] if pj.is_file() else []
        if not pj.is_file() and base.is_dir():
            for c in sorted(base.iterdir()):
                if c.is_dir() and c.name not in util.IGNORE_DIRS and not c.name.startswith(".") and (c / "package.json").is_file():
                    apps.append((c, pre + c.name + "/"))
        for abase, apre in apps:
            for sub in ("src/app", "app", "src/pages", "pages", "src/components", "components", "src/views", "views"):
                if (abase / sub).is_dir():
                    page.append(apre + sub + "/**")
            for sub in ("src/app/api", "app/api", "src/pages/api", "pages/api"):
                if (abase / sub).is_dir():
                    api.append(apre + sub + "/**")
            for f in ("src/middleware.ts", "middleware.ts", "src/middleware.js", "middleware.js"):
                if (abase / f).is_file():
                    api.append(apre + f)
        for dirpath, dirnames, _ in os.walk(str(base)):
            dirnames[:] = [x for x in dirnames if x not in util.IGNORE_DIRS and not x.startswith(".")]
            name = Path(dirpath).name
            rel = util.rel(root, dirpath)
            if re.search(r"(^|/)(tests?|__tests__)(/|$)", rel):
                continue
            if name in ("api", "routers", "routes", "endpoints", "controllers") and not pj.is_file():
                api.append(rel + "/**")
    # .sql이 있는 폴더는 그 안의 .sql만 DB 동작으로 — 폴더 전체로 잡으면 docs/ 아래 문서·이미지까지 DB 변경이 된다(C5)
    sql_dirs = [c["dir"] for c in (sql_patch or {}).get("candidates", [])]
    db += [(x + "/**/*.sql") if x != "." else "**/*.sql" for x in sql_dirs] or ["**/*.sql"]
    if sql_patch and sql_patch.get("suggested") and sql_patch["suggested"] + "/**/*.sql" not in db:
        db.append(sql_patch["suggested"] + "/**/*.sql")
    db += ["**/*repository*.py", "**/repositories/**"]
    out = {}
    if page:
        out["page"] = sorted(set(page))
    if api:
        out["api"] = sorted(set(api))
    out["db"] = sorted(set(db))
    return out


# ---------------------------------------------------------------- 모으기·저장

def collect(root, run_tests=True, map_data=None, use_llm=True, progress=None):
    """기계 사실 전부 → facts.json. 구조 지도도 여기서 만든다(필요하면)."""
    from vmc_harness.bootstrap import conventions, protected_suggest, structmap
    root = Path(root)
    say = progress or (lambda *_: None)
    facts = {}
    cfg = read_json(paths.harness_dir(root) / "config.json", {}) or {}
    if cfg.get("source_roots"):
        facts["source_roots"] = _fact(cfg["source_roots"], "read .harness/config.json#source_roots")
    else:
        sr, why = detect_source_roots(root)
        facts["source_roots"] = _fact(sr, why)
    source_roots = facts["source_roots"]["value"]
    say("명령 찾기")
    cmds = detect_commands(root, source_roots)
    facts["commands"] = _fact(cmds, "package.json scripts · pyproject [tool.*] · pytest.ini/tox.ini/setup.cfg · Makefile · scripts/*")
    for d in sorted(set(c["dir"] for c in cmds if c["name"] == "pytest")):
        py, src = find_python(root, d)
        facts.setdefault("python", _fact(py, "가상환경 탐색: %s" % src))
    if run_tests:
        say("테스트 기준선 (상한 %ss)" % config.get("bootstrap.test_timeout_s", root))
    else:
        say("테스트 수집만 (--no-tests)")
    tb = test_baseline(root, cmds, run_tests=run_tests)
    facts["test_baseline"] = _fact(tb, " ; ".join(x.get("command") or x.get("collect_command") or x.get("reason", "")
                                                   for x in tb) or "테스트 명령 없음")
    say("DB·SQL·환경")
    v, c = detect_db(root, source_roots)
    facts["db"] = _fact(v, c)
    v, c = detect_sql_patch_dir(root)
    facts["sql_patch_dir"] = _fact(v, c)
    v, c = detect_environment(root)
    facts["environment"] = _fact(v, c)
    v, c = detect_agents_md(root)
    facts["agents_md"] = _fact(v, c, {"kind": "exists", "path": "AGENTS.md"})
    facts["behavior_globs"] = _fact(detect_behavior_globs(root, source_roots, cmds, facts["sql_patch_dir"]["value"]),
                                    "소스 루트 아래 폴더 이름(app·pages·components·api·routers…)·SQL 폴더")
    say("구조 지도")
    if map_data is None:
        map_data = structmap.build(root, use_llm=use_llm, source_roots=source_roots, progress=lambda m: say("  " + m))
    facts["structure_map"] = _fact({"modules": len(map_data.get("modules", [])), "path": ".harness/cache/map.json",
                                    "head": map_data.get("head")}, "vmc-harness map")
    facts["conventions"] = _fact(conventions.detect(root, map_data), "vmc-harness map → AST·파일 이름 집계")
    facts["protected_candidates"] = _fact(protected_suggest.suggest(root, source_roots),
                                          "폴더·파일 이름 규칙 + AST(결재 함수) + 생성 파일 머리말")
    data = {"generated_at": now_iso(), "facts": facts}
    write_json(facts_path(root), data)
    return data


def _run_cmd(c, root):
    """루트에서 그대로 돌릴 수 있는 명령 문자열."""
    if c["source"].endswith("package.json"):
        return c["run"] if c["dir"] == "." else "npm --prefix %s run %s" % (c["dir"], c["name"])
    if c["name"] == "pytest":
        return "python -m pytest %s" % ("" if c["dir"] == "." else c["dir"]).strip()
    return c["run"]


def load(root):
    """평평한 dict (interfaces.md — gates·db·loop가 읽는다). 없으면 {}.

    키: source_roots, commands{kind:{command,dir}}, command_list[...], test_command·build_command·lint_command·typecheck_command,
        python, test_baseline{failures[...], runs[...]}, db(없으면 None)·db_kind, sql_patch_dir, environment,
        behavior_globs, conventions, protected_candidates, agents_md, structure_map, _commands{키: 읽은 방법}
    """
    data = read_json(facts_path(root)) or {}
    f = data.get("facts") or {}
    if not f:
        return {}
    out = {k: v.get("value") for k, v in f.items()}
    out["_commands"] = {k: v.get("command") for k, v in f.items()}
    out["_generated_at"] = data.get("generated_at")
    cl = out.get("commands") or []
    out["command_list"] = cl
    cmds = {}
    for kind in ("test", "build", "lint", "typecheck"):
        cands = [c for c in cl if c["kind"] == kind and not c["name"].startswith("pre")
                 and (kind != "test" or c["name"] in ("pytest", "test"))
                 and not c["source"].startswith(c["dir"] + "/scripts/") and not c["source"].startswith("scripts/")]
        if cands:
            c = cands[0]
            cmds[kind] = {"command": _run_cmd(c, root), "dir": c["dir"], "all": [_run_cmd(x, root) for x in cands]}
            out["%s_command" % kind] = cmds[kind]["command"]
    out["commands"] = cmds
    runs = out.get("test_baseline") or []
    failures = []
    for r in runs if isinstance(runs, list) else []:
        pre = "" if r.get("dir") in (None, ".") else r["dir"] + "/"
        for x in (r.get("failed") or []) + (r.get("errors") or []) + [e["path"] for e in r.get("collection_errors") or []]:
            failures += [x, pre + x] if pre else [x]
    out["test_baseline"] = {"failures": sorted(set(failures)), "runs": runs if isinstance(runs, list) else []}
    db = out.get("db") or {}
    out["db_kind"] = db.get("kind")
    if not db.get("kind"):
        out["db"] = None
    return out


def load_raw(root):
    return read_json(facts_path(root)) or {}


def recheck(root, force=False):
    """다시 읽기(P3 — 가끔). 테스트 실행은 빼고 결정적 읽기만. 바뀐 키 목록. 규칙 블록 재확인은 gates.rules.refresh_machine_facts가 한다."""
    data = load_raw(root)
    if not data:
        return []
    last = parse_iso(data.get("generated_at"))
    if not force and last and (now() - last).days < config.get("bootstrap.recheck_days", root):
        return []
    f = data["facts"]
    sr = (f.get("source_roots") or {}).get("value") or ["."]
    cmds = detect_commands(root, sr)
    sql = detect_sql_patch_dir(root)[0]
    new = {"commands": cmds, "db": detect_db(root, sr)[0], "sql_patch_dir": sql,
           "environment": detect_environment(root)[0], "agents_md": detect_agents_md(root)[0],
           "behavior_globs": detect_behavior_globs(root, sr, cmds, sql)}
    changed = []
    for k, v in new.items():
        old = (f.get(k) or {}).get("value")
        if json.dumps(old, sort_keys=True, ensure_ascii=False) != json.dumps(v, sort_keys=True, ensure_ascii=False):
            changed.append(k)
            f.setdefault(k, {})["value"] = v
        f.setdefault(k, {})["read_at"] = now_iso()
    data["generated_at"] = now_iso()
    write_json(facts_path(root), data)
    return changed


def _fail_count(runs):
    return sum(len(r.get("failed") or []) + len(r.get("errors") or []) + len(r.get("collection_errors") or [])
               for r in runs or [] if isinstance(r, dict))


def refresh_test_baseline(root):
    """재진단 때 테스트 기준선(실패 목록)을 다시 잰다(D5) — 그사이 고친 실패가 기준선에 남지 않게.
    잴 수 없던 실행(시간 초과·미실행)은 지난 값을 그대로 둔다. 반환 {"before": 실패 수, "after": 실패 수} | None"""
    data = load_raw(root)
    f = data.get("facts") if data else None
    if not f or not (f.get("commands") or {}).get("value"):
        return None
    old = (f.get("test_baseline") or {}).get("value") or []
    new = test_baseline(root, f["commands"]["value"], run_tests=True)
    by = {(r.get("runner"), r.get("dir")): r for r in old if isinstance(r, dict)}
    merged = []
    for r in new:
        prev = by.get((r.get("runner"), r.get("dir")))
        merged.append(prev if prev and r.get("status") in ("timeout", "not_run") and prev.get("status") in ("pass", "fail") else r)
    tb = f.setdefault("test_baseline", {})
    tb["value"] = merged
    tb["read_at"] = now_iso()
    tb["previous_failures"] = _fail_count(old)
    write_json(facts_path(root), data)
    return {"before": _fail_count(old), "after": _fail_count(merged)}


# ---------------------------------------------------------------- 규칙 블록용 환경 사실 → rules.json (gates.rules 형식)

FACT_COMMAND = ["vmc-harness", "init", "--print-fact"]  # gates.rules.refresh_machine_facts가 다시 실행하는 명령
_DB_LABEL = {"mssql": "MSSQL", "postgres": "PostgreSQL", "mysql": "MySQL", "oracle": "Oracle", "sqlite": "SQLite"}


def rule_values(facts):
    """기계로 못 막는 환경 사실만(02 §규칙 블록). {rule_id: (text 틀 — {value} 자리, 값)}"""
    out = {}
    db = facts.get("db") or {}
    if db.get("kind"):
        v = _DB_LABEL.get(db["kind"], db["kind"])
        lvl = ((db.get("db_facts") or {}).get("compatibility_level") or {}).get("value")
        if lvl is not None:
            v += ", 호환성 수준 %s" % lvl
            if db.get("unavailable_features"):
                v += "(쓸 수 없는 기능: %s)" % ", ".join(db["unavailable_features"])
        out["mf-db"] = ("DB: {value}", v)
    env = facts.get("environment") or {}
    parts = []
    if env.get("dev_windows"):
        parts.append("개발 Windows(%s)" % ", ".join(env["dev_windows"][:2]))
    if env.get("prod_linux"):
        parts.append("운영 Linux(%s)" % ", ".join(env["prod_linux"]))
    if parts:
        out["mf-os"] = ("환경: {value}", " · ".join(parts))
    if env.get("eol_rules"):
        out["mf-eol"] = ("줄 끝(.gitattributes): {value}", "; ".join(env["eol_rules"][:5]))
    by_dir = {}
    for c in facts.get("command_list") or []:
        if c["kind"] in ("build", "test", "lint", "typecheck") and not c["name"].startswith("pre") \
                and "/scripts/" not in "/" + c["source"]:
            by_dir.setdefault(c["dir"], []).append(c)
    for d, cs in sorted(by_dir.items()):
        seen, items = set(), []
        for c in cs:
            r = _run_cmd(c, None)
            if r in seen:
                continue
            seen.add(r)
            items.append("%s `%s`" % (c["kind"], r))
        out["mf-cmd-%s" % (d.replace("/", "_") if d != "." else "root")] = ("명령(%s): {value}" % d, ", ".join(items[:6]))
    for tb in (facts.get("test_baseline") or {}).get("runs") or []:
        if tb.get("runner") == "pytest" and tb.get("status") in ("pass", "fail", "collected"):
            ce = len(tb.get("collection_errors") or [])
            fl = len(tb.get("failed") or []) + len(tb.get("errors") or [])
            if tb["status"] == "pass" and not ce:
                v = "전체 통과가 기준"
            elif tb["status"] == "collected":
                v = "원래 수집 오류 %d건이 기준선(실행 결과 미확인) — 완료 기준은 새 실패 0건" % ce
            else:
                v = "원래 수집 오류 %d건·실패 %d건이 기준선 — 완료 기준은 새 실패 0건" % (ce, fl)
            out["mf-test-%s" % tb["dir"].replace("/", "_")] = ("테스트(%s): {value}" % tb["dir"], v)
    return out


def print_fact(root, rule_id):
    """`vmc-harness init --print-fact <rule_id>` — 그 규칙 값을 지금 다시 읽어 한 줄로. 없으면 None."""
    recheck(root, force=True)
    v = rule_values(load(root)).get(rule_id)
    return v[1] if v else None


def _rules_api():
    try:
        from vmc_harness.gates import rules as grules
        return grules
    except ImportError:
        return None


def read_rules(root):
    g = _rules_api()
    if g:
        return g.load(root)
    data = read_json(rules_path(root), None) or {}
    data.setdefault("rules", [])
    return data


def _save_rules(root, data):
    g = _rules_api()
    if g:
        g.save(root, data)
    else:
        write_json(rules_path(root), data)


def write_machine_fact_rules(root, facts):
    """rules.json의 bootstrap 출처 machine_fact 규칙만 갈아 끼운다(사람 명시 요청·다른 출처 규칙은 그대로)."""
    data = read_rules(root)
    mine = lambda r: (r.get("basis") or {}).get("type") == "machine_fact" and (r.get("basis") or {}).get("source") == "bootstrap"
    old_by = {r["id"]: r for r in data["rules"] if mine(r)}
    keep = [r for r in data["rules"] if not mine(r)]
    new_rules = []
    for rid, (text, value) in rule_values(facts).items():
        prev = old_by.get(rid) or {}
        same = prev.get("value") == value and prev.get("text") == text
        new_rules.append({"id": rid, "text": text, "value": value,
                          "basis": {"type": "machine_fact", "command": FACT_COMMAND + [rid], "source": "bootstrap"},
                          "checked_at": now_iso(), "created_at": prev.get("created_at") if same else now_iso(),
                          "enabled": prev.get("enabled", True)})
    data["rules"] = keep + new_rules
    _save_rules(root, data)
    return new_rules
