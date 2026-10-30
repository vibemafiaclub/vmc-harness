"""운영 접속 설정 파일 보호 (spec 02 §운영 접속 설정) — PC에 있는 운영 접속 정보를 에이전트가 찾아 쓰지 못하게.

"운영 접속 설정 파일" = 이름(파일·바로 위 폴더)에 운영 표시(`db.prod_name_patterns` 재사용, db.client.looks_production)가 있고
내용에 접속 정보 형태(Server=…;·Password=·DATABASE_URL·mssql://·ConnectionStrings 등)가 있는 설정 파일.
- 막는 것(확신 높음): 그런 파일을 Read·Grep 도구나 셸 읽기 명령(cat·type·Get-Content·grep·cp 원본…)으로 읽기,
  `--env-file <그 파일>`로 실행, 운영 값 환경 변수(ASPNETCORE_ENVIRONMENT=Production·NODE_ENV=production…)와 함께 앱을 띄우는데
  그 값이 불러올 운영 접속 설정 파일이 실제로 있을 때. 보호 영역(protected.json kind=prod_config)으로 확정된 파일은 이름·내용 판정 없이 막는다
- 기록만(확신 낮음 — 01 P2 오탐 0 우선): 이름만 운영이고 접속 정보 형태가 없는 파일, 운영 값 환경 변수로 앱을 띄우지만 불러올 파일을 못 찾은 경우,
  그 파일이 명령에 보이지만 읽기인지 모르는 경우(git diff·인라인 스크립트 등) → events `prod_config_seen`
- 사람이 푸는 칸: `.harness/config.json` `prod_allow.files`(경로 glob)·`prod_allow.names`(서버·DB 이름). 늘리기는 사람 승인(메타 게이트)
- 판정 중 예외는 막지 않고 기록만(gate_error) — 이름·내용 추정 게이트라 하네스 고장으로 작업을 멈추지 않는다
"""

import os
import re
from pathlib import Path

from vmc_harness.core import config
from vmc_harness.gates import globs, shell
from vmc_harness.gates.common import block, facts, log_event

NAME = "prod_config"
KIND = "prod_config"
CONFIG_EXT = {".json", ".yaml", ".yml", ".toml", ".ini", ".config", ".xml", ".properties", ".conf", ".cfg", ".env"}
TEMPLATE = re.compile(r"(?i)(example|sample|template|\.dist$|\.tpl$|placeholder|schema)")
CONN_INFO = [
    re.compile(r"(?i)\b(server|data\s+source|host|addr|address)\s*=\s*[^;\s'\"]+\s*;"),
    re.compile(r"(?i)\b(password|pwd)\s*=\s*[^;\s'\"]+"),
    re.compile(r"(?im)^\s*(export\s+)?(DATABASE_URL|DB_URL|DB_CONNECTION|DB_HOST|DB_PASSWORD|DB_PASS|SQLALCHEMY_DATABASE_URI"
               r"|CONNECTION_STRING|ConnectionStrings__\w+|MSSQL_\w+|SQLSERVER_\w+)\s*[=:]\s*\S"),
    re.compile(r"(?i)\b(mssql|sqlserver|postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|oracle)(\+\w+)?://"),
    re.compile(r"(?i)\bjdbc:\w+:"),
    re.compile(r"(?i)[\"']?ConnectionStrings[\"']?\s*:"),
    re.compile(r"(?i)\bconnection_?string[\"']?\s*[=:]"),
]
READ_CMDS = {"cat", "type", "more", "less", "head", "tail", "bat", "nl", "strings", "xxd", "od", "hexdump",
             "get-content", "gc", "cut", "sort", "uniq", "awk", "gawk", "sed", "grep", "egrep", "fgrep", "rg", "ag",
             "findstr", "select-string", "sls", "jq", "yq", "base64", "dotenv-cli"}
ENV_VARS = {"aspnetcore_environment", "dotnet_environment", "node_env", "app_env", "rails_env", "flask_env", "env",
            "environment", "spring_profiles_active", "stage", "deploy_env", "python_env", "django_env"}
APP_RUNNERS = {"node", "nodemon", "ts-node", "tsx", "bun", "deno", "python", "python3", "py", "uvicorn", "gunicorn",
               "hypercorn", "daphne", "flask", "celery", "waitress-serve", "pm2", "java", "rails", "php", "ruby"}
PM = {"npm", "pnpm", "yarn"}
PM_LAUNCH = {"start", "dev", "serve", "preview", "start:prod", "prod"}


# ---------------------------------------------------------------- 파일 판정

def _allowed(root, rel_or_abs):
    try:
        from vmc_harness.db.client import prod_allow
        pats = prod_allow(root)["files"]
    except Exception:
        pats = []
    p = globs.norm(rel_or_abs)
    return any(globs.match(p, g) or globs.match(os.path.basename(p), g) for g in pats)


def _looks_prod(root, *names):
    try:
        from vmc_harness.db.client import looks_production
        return looks_production(root, *names, strict=True)
    except Exception:
        return None


def config_like(name):
    low = name.lower()
    if low.startswith(".env") or low.endswith(".env"):
        return True
    return Path(low).suffix in CONFIG_EXT


def connection_info(text):
    return any(rx.search(text or "") for rx in CONN_INFO)


def name_prod(root, path, rel=None):
    """파일 이름(또는 프로젝트 안 바로 위 폴더 이름)에 운영 표시가 있는 설정 파일인가(템플릿 이름은 아니다).
    프로젝트 밖·프로젝트 폴더 자체 이름은 보지 않는다(사용자 폴더 이름 등에 걸리는 오탐)."""
    p = Path(str(path).replace("\\", "/"))
    if not config_like(p.name) or TEMPLATE.search(p.name):
        return False
    names = [p.name.lstrip(".")]
    if rel and "/" in rel:
        names.append(rel.rsplit("/", 2)[-2])
    return bool(_looks_prod(root, *names))


def _read_head(abs_path, root):
    try:
        with open(str(abs_path), "r", encoding="utf-8", errors="replace") as f:
            return f.read(int(config.get("gates.prod_config_scan_bytes", root)))
    except OSError:
        return None


def _protected_globs(root):
    try:
        from vmc_harness.gates import protected
        return [e["glob"] for e in protected.entries(root) if e.get("kind") == KIND]
    except Exception:
        return []


def classify(root, abs_path, rel=None):
    """'high'(막는다) | 'low'(기록만) | None(무관)."""
    rel = rel if rel is not None else (globs.rel_to(root, abs_path) if root else None)
    key = rel or str(abs_path)
    if rel and any(globs.match(rel, g) for g in _protected_globs(root)):
        return None if _allowed(root, key) else "high"
    if not name_prod(root, abs_path, rel) or _allowed(root, key):
        return None
    if not os.path.isfile(str(abs_path)):
        return None
    text = _read_head(abs_path, root)
    if text is None:
        return None
    return "high" if connection_info(text) else "low"


def known_files(root):
    """운영 접속 설정 파일로 알려진 것(보호 영역 확정분 + init 후보) — 루트 기준 상대 경로."""
    out = []
    for c in (facts(root).get("protected_candidates") or []):
        if isinstance(c, dict) and c.get("category") == KIND:
            out.extend(c.get("examples") or [c.get("glob")])
    for g in _protected_globs(root):
        if "*" not in g:
            out.append(g)
    seen = []
    for x in out:
        if x and x not in seen:
            seen.append(x)
    return seen


# ---------------------------------------------------------------- 셸 명령 판정

def _shell_tokens(command, depth=0):
    """[(cmd, args, env)] — 셸 안 셸(bash -c·cmd /c·pwsh -Command)도 펼친다. env는 그 명령까지 쌓인 환경 변수 지정."""
    out, env = [], {}
    if depth > int(config.get("gates.shell_max_nest")):
        return out
    for seg in shell._segments(command):
        seg = list(seg)
        # PowerShell `$env:X="v"` / cmd `set X=v` / `export X=v` — 같은 명령줄 뒤 명령에 이어진다
        if seg and seg[0].lower().startswith("$env:") and "=" in "".join(seg):
            k, _, v = "".join(seg)[len("$env:"):].partition("=")
            env[k.strip().lower()] = v.strip().strip("'\"")
            continue
        if seg and seg[0].lower() in ("set", "export", "setx") and len(seg) > 1:
            for t in seg[1:]:
                if "=" in t:
                    k, _, v = t.partition("=")
                    env[k.strip().lower()] = v.strip().strip("'\"")
            continue
        local = dict(env)
        while seg and ("=" in seg[0] and not seg[0].startswith("-") and seg[0].split("=")[0].replace("_", "").isalnum()):
            k, _, v = seg[0].partition("=")
            local[k.lower()] = v.strip("'\"")
            seg = seg[1:]
        while seg and shell._base(seg[0]) in shell.PREFIX_CMDS | {"cross-env", "dotenv"}:
            seg = seg[1:]
            while seg and (seg[0].startswith("-") or ("=" in seg[0] and not seg[0].startswith("-"))):
                if "=" in seg[0] and not seg[0].startswith("-"):
                    k, _, v = seg[0].partition("=")
                    local[k.lower()] = v.strip("'\"")
                seg = seg[1:]
        if not seg:
            continue
        cmd, args = shell._base(seg[0]), seg[1:]
        if cmd in shell.SHELLS:
            inner = shell._flag_value(args, ("-c", "-lc", "-ic", "-xc"))
            if inner is not None:
                out.extend(_shell_tokens(inner, depth + 1))
                continue
        if cmd == "cmd":
            for i, a in enumerate(args):
                if a.lower() in ("/c", "/k"):
                    out.extend(_shell_tokens(" ".join(args[i + 1:]), depth + 1))
                    break
            continue
        if cmd in shell.POWERSHELLS:
            inner = shell._flag_value(args, ("-command", "-c"), ci=True)
            if inner is not None:
                out.extend(_shell_tokens(inner, depth + 1))
                continue
        out.append((cmd, args, local, seg))
    return out


def _env_files(cmd, args):
    out = []
    for i, a in enumerate(args):
        if a in ("--env-file", "--env_file", "--dotenv") and i + 1 < len(args):
            out.append(args[i + 1])
        elif a.startswith("--env-file=") or a.startswith("--dotenv="):
            out.append(a.split("=", 1)[1])
    return out


def _is_launch(cmd, args):
    """앱·서버 실행인가(테스트·빌드는 아니다)."""
    if cmd == "dotnet":
        return bool(args) and (args[0] in ("run", "watch") or args[0].lower().endswith(".dll"))
    if cmd in PM:
        na = [a for a in args if not a.startswith("-")]
        if not na:
            return False
        if na[0] in ("run", "run-script") and len(na) > 1:
            return na[1] in PM_LAUNCH
        return na[0] in PM_LAUNCH
    if cmd in ("next", "nest", "nuxt", "vite", "astro", "remix"):
        return bool(args) and args[0] in ("start", "dev", "preview", "serve")
    if cmd in ("docker", "podman"):
        return bool(args) and (args[0] == "run" or (args[0] == "compose" and "up" in args))
    if cmd in ("docker-compose",):
        return "up" in args
    if cmd in ("mvn", "mvnw", "gradle", "gradlew"):
        return any(a in ("spring-boot:run", "bootRun") for a in args)
    if cmd in APP_RUNNERS:
        na = [a for a in args if not a.startswith("-")]
        if cmd in ("python", "python3", "py") and "-m" in args:
            i = args.index("-m")
            mod = args[i + 1] if i + 1 < len(args) else ""
            return mod in ("uvicorn", "gunicorn", "flask", "django", "celery", "hypercorn", "waitress")
        if cmd in ("python", "python3", "py", "node", "bun", "deno", "ts-node", "tsx", "ruby", "php"):
            if not na or cmd in ("bun", "deno") and na[0] in ("test", "build", "install", "add"):
                return False
            first = os.path.basename(na[0]).lower()
            if re.search(r"(?i)(test|spec|lint|build|check)", first) and first != "manage.py":
                return False
            if first == "manage.py":
                return len(na) > 1 and na[1] in ("runserver", "shell", "migrate", "dbshell", "runscript")
            return True
        return True
    return False


def _launch_env(root, local, args):
    """운영 값을 가진 환경 지정 (변수, 값) 또는 None."""
    for k, v in local.items():
        if k in ENV_VARS and v and _looks_prod(root, v):
            return k, v
    for i, a in enumerate(args):
        if a.lower() in ("--environment", "--env") and i + 1 < len(args) and _looks_prod(root, args[i + 1]):
            return a, args[i + 1]
    return None


def _resolve(ctx, raw_path):
    rawp = os.path.expandvars(os.path.expanduser(str(raw_path).strip("'\"")))
    if os.sep == "/":
        rawp = rawp.replace("\\", "/")
    ab = str(globs.abspath(rawp, ctx.cwd or ctx.root))
    rel = globs.rel_to(ctx.root, ab) if ctx.root else None
    return ab, rel


def _label(rel, ab):
    return rel or os.path.basename(ab)


def _seen(ctx, action, path, why):
    log_event(ctx.root, "prod_config_seen", action=action, path=path, why=why, tool=ctx.tool_name,
              session_id=ctx.session_id, task_id=ctx.task_id, blocked=False)


def _hit(ctx, raw_path):
    ab, rel = _resolve(ctx, raw_path)
    return classify(ctx.root, ab, rel), _label(rel, ab)


def _launch_files(ctx, value):
    """그 환경 값이 불러올 운영 접속 설정 파일(알려진 것 + 루트·현재 폴더 바로 아래)."""
    v = value.lower()
    cands = list(known_files(ctx.root))
    for base in {ctx.root, ctx.cwd or ctx.root}:
        try:
            for n in os.listdir(str(base)):
                if v in n.lower() and config_like(n):
                    cands.append(globs.rel_to(ctx.root, str(Path(base) / n)) or n)
        except OSError:
            pass
    out = []
    for rel in cands:
        if v not in os.path.basename(rel).lower():
            continue
        ab = str(Path(ctx.root) / rel)
        if classify(ctx.root, ab, rel) == "high" and rel not in out:
            out.append(rel)
    return out


def check(ctx):
    """HookResult(block) 또는 None. 확신 낮은 경우는 기록만."""
    if not ctx.root:
        return None
    ti = ctx.tool_input or {}
    if ctx.tool_name in ("Read", "Grep", "NotebookRead"):
        p = ti.get("file_path") or ti.get("path") or ti.get("notebook_path")
        if not p or (ctx.tool_name == "Grep" and not os.path.isfile(str(globs.abspath(str(p), ctx.cwd or ctx.root)))):
            return None
        lvl, label = _hit(ctx, p)
        if lvl == "high":
            return block("운영 접속 정보 파일 읽기 차단됨(%s)" % label, "운영 접속 정보 파일 읽기 차단됨")
        if lvl == "low":
            _seen(ctx, "read", label, "name_only")
        return None
    if not ctx.command:
        return None
    for cmd, args, local, seg in _shell_tokens(ctx.command):
        for f in _env_files(cmd, args):
            lvl, label = _hit(ctx, f)
            if lvl == "high":
                return block("운영 접속 정보 파일로 실행 차단됨(%s)" % label, "운영 접속 정보 파일로 실행 차단됨")
            if lvl == "low":
                _seen(ctx, "env_file", label, "name_only")
        if cmd in READ_CMDS or cmd in shell.COPY_CMDS:
            na = shell._nonopt(args, cmd)
            if cmd in shell.COPY_CMDS:
                na = na[:-1]
            for i, t in enumerate(args):
                if t == "<" and i + 1 < len(args):
                    na.append(args[i + 1])
            pg = _protected_globs(ctx.root) if na else []
            for t in na:
                if not pg and not config_like(os.path.basename(t.replace("\\", "/"))):
                    continue
                lvl, label = _hit(ctx, t)
                if lvl == "high":
                    return block("운영 접속 정보 파일 읽기 차단됨(%s)" % label, "운영 접속 정보 파일 읽기 차단됨")
                if lvl == "low":
                    _seen(ctx, "read", label, "name_only")
        elif _is_launch(cmd, args):
            env = _launch_env(ctx.root, local, args)
            if env:
                files = _launch_files(ctx, env[1])
                if files:
                    var = env[0] if env[0].startswith("-") else env[0].upper()
                    shown = ", ".join(files[:int(config.get("gates.reason_max_files"))])
                    return block("운영 설정으로 앱 실행 차단됨(%s=%s · %s)" % (var, env[1], shown), "운영 설정으로 앱 실행 차단됨")
                _seen(ctx, "launch", "%s=%s" % (env[0], env[1]), "no_prod_config_file")
        else:
            for t in seg[1:]:
                if t.startswith("-") or not config_like(os.path.basename(t.replace("\\", "/"))):
                    continue
                lvl, label = _hit(ctx, t)
                if lvl:
                    _seen(ctx, "mention", label, "%s:%s" % (cmd, lvl))
    return None


def safe_check(ctx):
    """예외는 막지 않고 기록만(이 게이트는 추정 — 오탐보다 놓침을 택한다, 01 P2)."""
    try:
        return check(ctx)
    except Exception as e:
        log_event(ctx.root, "gate_error", module="gates.prod_config", event=ctx.event,
                  error=("%s: %s" % (type(e).__name__, e))[:300])
        return None
