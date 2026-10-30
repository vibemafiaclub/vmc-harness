"""검증 게이트 소요 실측 (spec 02 §검증 게이트 소요, 04 참고 지표) — 고객 쪽 테스트·빌드·린트·타입체크와 git commit 훅.

에이전트가 셸 도구로 부른 명령이 검증 명령이면 시작·끝 시각·종료 코드를 로컬 이벤트 `verify_run`으로 남긴다(원문 명령은 남기지 않고 게이트 이름만).
- 판별: bootstrap facts가 아는 프로젝트 명령(package.json scripts·Makefile·pyproject·scripts/*)이 먼저 — facts가 그 이름을 검증 명령이 아닌
  것(dev·other)으로 알면 패턴으로 다시 보지 않는다. facts에 없으면 보수적 패턴(pytest·jest·tsc·eslint·dotnet test… 하위 명령까지 맞을 때만)
- `git commit`은 그 안에서 도는 git 훅(pre-commit·commit-msg 등) 소요를 명령 전체 소요로 근사한다(`approx=true`, 이름에 "근사").
  고객 쪽 커밋 훅이 없거나 `--no-verify`면 남기지 않는다(하네스 자신의 prepare-commit-msg만 있으면 고객 게이트가 아니다)
- 소요: Claude Code PostToolUse·PostToolUseFailure의 `duration_ms`(2.1.308 실측)를 쓰고, 없으면(Codex) PreToolUse에 적어 둔 시작 시각과의 차이
- 종료 코드: Claude는 성공이면 PostToolUse(0), 실패면 PostToolUseFailure(`error: "Exit code N"`). Codex는 tool_response에 있으면 쓰고 없으면 null
- 백그라운드 실행(run_in_background)·감시 모드(--watch)는 소요가 의미 없어 남기지 않는다
이 기록은 판정을 움직이지 않는다 — 주간 집계(ledger.metrics.verification_wait)가 "평균 소요 × 횟수 = 주당 대기"와 재배치 후보(제안 문구)로만 쓴다.
"""

import hashlib
import os
import re
import time
from pathlib import Path

from vmc_harness.core import config, paths
from vmc_harness.core.clock import now
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.gates import shell
from vmc_harness.gates.common import facts, log_event

EVENT = "verify_run"
KINDS = ("test", "build", "lint", "typecheck")
COMMIT_KIND = "commit_hooks"
PM = {"npm", "pnpm", "yarn", "bun"}
PM_BUILTIN = {"install", "i", "ci", "add", "remove", "rm", "uninstall", "exec", "dlx", "x", "create", "init", "publish",
              "link", "outdated", "update", "upgrade", "up", "why", "list", "ls", "info", "view", "audit", "config",
              "cache", "store", "dedupe", "rebuild", "prune", "pack", "version", "help", "login", "whoami", "set",
              "global", "start", "restart", "stop", "env", "root", "bin", "prefix", "fund", "doctor", "workspaces"}
PM_VALUED = {"--prefix", "-C", "--dir", "--filter", "-F", "--workspace", "-w", "--cwd"}
WRAPPERS = {"uv": "run", "poetry": "run", "pipenv": "run", "pdm": "run", "hatch": "run", "rye": "run", "pixi": "run",
            "npx": None, "pnpx": None, "bunx": None}
PY = re.compile(r"^(python(\d+(\.\d+)?)?|py)$")
# 도구 이름 → 종류. 값이 dict면 하위 명령이 맞을 때만(보수적)
TOOLS = {
    "pytest": "test", "py.test": "test", "unittest": "test", "tox": "test", "nox": "test", "jest": "test",
    "vitest": "test", "mocha": "test", "ava": "test", "phpunit": "test", "rspec": "test", "karma": "test",
    "playwright": {"test": "test"}, "cypress": {"run": "test"},
    "ruff": "lint", "flake8": "lint", "pylint": "lint", "eslint": "lint", "stylelint": "lint", "biome": "lint",
    "golangci-lint": "lint", "rubocop": "lint",
    "mypy": "typecheck", "pyright": "typecheck", "vue-tsc": "typecheck",
    "dotnet": {"build": "build", "test": "test", "format": "lint"},
    "go": {"test": "test", "build": "build", "vet": "lint"},
    "cargo": {"test": "test", "build": "build", "clippy": "lint", "check": "typecheck"},
    "mvn": {"test": "test", "verify": "test", "package": "build", "compile": "build", "install": "build"},
    "gradle": {"test": "test", "build": "build", "check": "test", "assemble": "build"},
    "msbuild": "build", "webpack": "build",
    "next": {"build": "build", "lint": "lint"}, "vite": {"build": "build"}, "ng": {"test": "test", "build": "build", "lint": "lint"},
}
CHECK_ONLY = {"black": "lint", "prettier": "lint", "isort": "lint"}   # --check일 때만(아니면 고치기 명령)
CUSTOMER_HOOKS = ("pre-commit", "commit-msg", "prepare-commit-msg", "post-commit", "pre-merge-commit")
PREFILTER = set(TOOLS) | set(CHECK_ONLY) | PM | set(WRAPPERS) | {"make", "git", "tsc", "gradlew", "mvnw", "bash", "sh",
                                                                 "pwsh", "powershell", "cmd", "zsh", "node", "env"}


# ---------------------------------------------------------------- 판별

def _watch(args):
    return any(a.startswith("--watch") or a == "watch" for a in args)


def _facts_entries(root):
    return [c for c in (facts(root).get("command_list") or []) if isinstance(c, dict)]


def _facts_kind(entries, pred):
    """facts에서 찾은 첫 항목의 종류(없으면 None, 찾았지만 검증 명령이 아니면 '')."""
    for c in entries:
        if pred(c):
            return c.get("kind") if c.get("kind") in KINDS else ""
    return None


def _kind_of(name):
    from vmc_harness.bootstrap.facts import _kind_of as k   # facts와 같은 이름 규칙(로직 복제 금지)
    return k(name)


def _pm_script(pm, args):
    i, na = 0, []
    while i < len(args):
        a = args[i]
        if a in PM_VALUED:
            i += 2
            continue
        if not a.startswith("-"):
            na.append(a)
        i += 1
    if not na:
        return None
    if na[0] in ("run", "run-script", "rs"):
        return na[1] if len(na) > 1 else None
    if na[0] in ("test", "t", "tst"):
        return "test"
    if pm != "npm" and na[0] not in PM_BUILTIN:
        return na[0]
    return None


def _segment(root, cmd, args, entries):
    """(kind, label, source) 또는 None."""
    if cmd in WRAPPERS:
        want = WRAPPERS[cmd]
        rest = list(args)
        if want:
            if not rest or rest[0] != want:
                return None
            rest = rest[1:]
        while rest and rest[0].startswith("-"):
            rest = rest[1:]
        if not rest:
            return None
        return _segment(root, shell._base(rest[0]), rest[1:], entries)
    if _watch(args):
        return None
    if cmd in PM:
        script = _pm_script(cmd, args)
        if cmd == "bun" and args[:1] == ["test"]:
            return "test", "bun test", "pattern"
        if not script:
            return None
        label = "%s run %s" % (cmd, script) if script != "test" else "%s test" % cmd
        fk = _facts_kind(entries, lambda c: c.get("name") == script and str(c.get("source", "")).endswith("package.json"))
        if fk is not None:
            return (fk, label, "facts") if fk else None
        k = _kind_of(script)
        return (k, label, "pattern") if k in KINDS else None
    if cmd == "make":
        i, target = 0, None
        while i < len(args):
            a = args[i]
            if a in ("-C", "-f", "-j", "--directory", "--file"):
                i += 2
                continue
            if not a.startswith("-") and "=" not in a:
                target = a
                break
            i += 1
        if not target:
            return None
        fk = _facts_kind(entries, lambda c: c.get("name") == target and str(c.get("source", "")).endswith("Makefile"))
        if fk is not None:
            return (fk, "make %s" % target, "facts") if fk else None
        k = _kind_of(target)
        return (k, "make %s" % target, "pattern") if k in KINDS else None
    if PY.match(cmd):
        if "-m" in args:
            i = args.index("-m")
            if i + 1 < len(args):
                return _segment(root, args[i + 1], args[i + 2:], entries)
            return None
        na = [a for a in args if not a.startswith("-")]
        if na and os.path.basename(na[0]) == "manage.py" and na[1:2] == ["test"]:
            return "test", "manage.py test", "pattern"
        return _script(na[0], entries) if na else None
    if cmd in ("bash", "sh", "zsh", "node", "pwsh", "powershell"):
        na = [a for a in args if not a.startswith("-")]
        return _script(na[0], entries) if na else None
    if cmd == "tsc":
        return ("typecheck" if "--noEmit" in args else "build"), "tsc", "pattern"
    if cmd in ("gradlew", "mvnw"):
        cmd = cmd[:-1] if cmd == "gradlew" else "mvn"
    if cmd in CHECK_ONLY:
        return (CHECK_ONLY[cmd], cmd, "pattern") if any(a in ("--check", "-c", "--check-only", "--diff") for a in args) else None
    t = TOOLS.get(cmd)
    if t is None:
        return _script(cmd, entries) if ("/" in cmd or "\\" in cmd) else None
    if isinstance(t, dict):
        sub = next((a for a in args if not a.startswith("-")), None)
        k = t.get(sub)
        return (k, "%s %s" % (cmd, sub), "pattern") if k else None
    return t, cmd, "pattern"


def _script(path, entries):
    """scripts/* 같은 프로젝트 스크립트 — facts에 있을 때만(패턴으로 추정하지 않는다)."""
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    for c in entries:
        run = str(c.get("run") or "")
        if run and c.get("source") == run and (p == run or p.endswith("/" + run)):
            return (c["kind"], run, "facts") if c.get("kind") in KINDS else None
    return None


def _git_dir(root):
    g = Path(root) / ".git"
    if g.is_file():
        try:
            line = g.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if line.startswith("gitdir:"):
            d = Path(line.split(":", 1)[1].strip())
            d = d if d.is_absolute() else (Path(root) / d)
            cd = d / "commondir"
            if cd.is_file():
                c = Path(cd.read_text(encoding="utf-8").strip())
                return c if c.is_absolute() else (d / c)
            return d
        return None
    return g if g.is_dir() else None


def customer_commit_hooks(root):
    """고객 쪽 커밋 훅이 있나 — 하네스 자신의 prepare-commit-msg만 있으면 False."""
    r = Path(root)
    if any((r / n).exists() for n in (".husky", ".pre-commit-config.yaml", "lefthook.yml", ".lefthook.yml", "lefthook.yaml")):
        return True
    gd = _git_dir(root)
    if not gd:
        return False
    try:
        if "hookspath" in (gd / "config").read_text(encoding="utf-8", errors="replace").lower():
            return True
    except OSError:
        pass
    from vmc_harness.task.trailers import HOOK_MARK
    for n in CUSTOMER_HOOKS + ("prepare-commit-msg.local",):
        h = gd / "hooks" / n
        if h.is_file():
            try:
                body = h.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if HOOK_MARK not in body:   # 하네스 훅은 고객 게이트가 아니다(고객 기존 훅은 .local로 옮겨 이어 부른다)
                return True
    return False


def _git_commit(root, args):
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-C", "-c") else 1
    if i >= len(args) or args[i] != "commit":
        return None
    rest = args[i + 1:]
    if any(a in ("-n", "--no-verify") for a in rest):
        return None
    if not customer_commit_hooks(root):
        return None
    return COMMIT_KIND, "git commit (훅 포함 근사)", "approx"


def _segments(command, depth=0):
    out = []
    if depth > int(config.get("gates.shell_max_nest")):
        return out
    for seg in shell._segments(command):
        while seg and ("=" in seg[0] and not seg[0].startswith("-") and seg[0].split("=")[0].replace("_", "").isalnum()):
            seg = seg[1:]
        while seg and shell._base(seg[0]) in shell.PREFIX_CMDS | {"cross-env"}:
            seg = seg[1:]
            while seg and (seg[0].startswith("-") or "=" in seg[0]):
                seg = seg[1:]
        if not seg:
            continue
        cmd, args = shell._base(seg[0]), seg[1:]
        if cmd in shell.SHELLS or cmd in shell.POWERSHELLS or cmd == "cmd":
            inner = shell._flag_value(args, ("-c", "-lc", "-ic", "-command"), ci=True)
            if inner is None and cmd == "cmd":
                for j, a in enumerate(args):
                    if a.lower() in ("/c", "/k"):
                        inner = " ".join(args[j + 1:])
                        break
            if inner is not None:
                out.extend(_segments(inner, depth + 1))
                continue
        out.append((cmd, args))
    return out


def classify(root, command):
    """검증 명령이면 {'gate','kinds','source','approx'}, 아니면 None. 여러 검증 명령이 이어지면 이름을 ' + '로 잇는다."""
    if not command:
        return None
    segs = _segments(command)
    if not any(c in PREFILTER or "/" in c or "\\" in c or PY.match(c) for c, _ in segs):
        return None
    entries = None
    hits = []
    for cmd, args in segs:
        if cmd == "git":
            h = _git_commit(root, args)
        else:
            if entries is None:
                entries = _facts_entries(root)
            h = _segment(root, cmd, args, entries)
        if h:
            hits.append(h)
    if not hits:
        return None
    return {"gate": " + ".join(h[1] for h in hits), "kinds": [h[0] for h in hits],
            "source": "facts" if any(h[2] == "facts" for h in hits) else ("approx" if all(h[2] == "approx" for h in hits) else "pattern"),
            "approx": any(h[2] == "approx" for h in hits)}


# ---------------------------------------------------------------- 시작 시각(Pre) → 소요(Post)

def _pending_file(root, session_id):
    sid = (session_id or "nosession").replace("/", "_").replace("\\", "_")
    return paths.cache_dir(root) / "gates" / ("verify-%s.json" % sid)


def _key(ctx):
    tid = (ctx.raw or {}).get("tool_use_id")
    if tid:
        return str(tid)
    return "cmd-" + hashlib.sha256((ctx.command or "").encode("utf-8")).hexdigest()[:16]


def _background(ctx):
    return bool((ctx.tool_input or {}).get("run_in_background"))


def on_pre(ctx):
    """PreToolUse(막히지 않은 셸 호출) — 검증 명령이면 시작 시각을 적어 둔다."""
    if ctx.tool_name != "Bash" or not ctx.command or _background(ctx):
        return None
    hit = classify(ctx.root, ctx.command)
    if not hit:
        return None
    f = _pending_file(ctx.root, ctx.session_id)
    st = read_json(f, None) or {}
    limit = float(config.get("gates.verify_pending_max_s", ctx.root))
    t = time.time()
    st = {k: v for k, v in st.items() if isinstance(v, dict) and t - float(v.get("t") or 0) <= limit}
    st[_key(ctx)] = dict(hit, t=t, started_at=now().isoformat())
    write_json(f, st)
    return hit


def _pop(ctx):
    f = _pending_file(ctx.root, ctx.session_id)
    if not f.is_file():
        return None
    st = read_json(f, None) or {}
    ent = st.pop(_key(ctx), None)
    write_json(f, st)
    return ent


_EXIT_TEXT = re.compile(r"(?i)exit(?:ed)?[\s_]*(?:code|status)?[\s:=]+(-?\d+)")


def exit_code(raw, failed):
    """(exit_code 또는 None, interrupted)."""
    raw = raw or {}
    if failed:
        if raw.get("is_interrupt"):
            return None, True
        m = _EXIT_TEXT.search(str(raw.get("error") or ""))
        return (int(m.group(1)) if m else None), False
    tr = raw.get("tool_response")
    if isinstance(tr, dict):
        if tr.get("interrupted"):
            return None, True
        for k in ("exit_code", "exitCode", "returncode", "return_code", "exit_status", "status"):
            v = tr.get(k)
            if isinstance(v, int) and not isinstance(v, bool):
                return v, False
        out = tr.get("output") or tr.get("formatted_output") or ""
        m = _EXIT_TEXT.search(str(out)[-400:]) if isinstance(out, str) else None
        if m:
            return int(m.group(1)), False
        if "stdout" in tr and "interrupted" in tr:
            return 0, False   # Claude Code: 실패는 PostToolUseFailure로 따로 온다(2.1.308 실측) — PostToolUse면 성공
        return None, False
    if isinstance(tr, str):
        m = _EXIT_TEXT.search(tr[-400:])
        return (int(m.group(1)) if m else None), False
    return None, False


def _duration_s(raw):
    raw = raw or {}
    tr = raw.get("tool_response") if isinstance(raw.get("tool_response"), dict) else {}
    for v in (raw.get("duration_ms"), tr.get("duration_ms"), tr.get("durationMs")):
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
            return float(v) / 1000.0
    return None


def on_post(ctx, failed=False):
    """PostToolUse·PostToolUseFailure — 소요·종료 코드를 이벤트로. 남긴 기록(없으면 None)."""
    if ctx.tool_name != "Bash" or not ctx.command or _background(ctx):
        return None
    ent = _pop(ctx)
    hit = ent or classify(ctx.root, ctx.command)
    if not hit:
        return None
    end = time.time()
    sec, timing = _duration_s(ctx.raw), "agent"
    if sec is None:
        if not ent:
            return None
        sec, timing = max(end - float(ent.get("t") or end), 0.0), "pre_post"
    rc, interrupted = exit_code(ctx.raw, failed)
    import datetime as _dt
    ended = now()
    started = (ent or {}).get("started_at") or (ended - _dt.timedelta(seconds=sec)).isoformat()
    return log_event(ctx.root, EVENT, gate=hit["gate"], kinds=hit["kinds"], source=hit["source"], approx=bool(hit.get("approx")),
                     started_at=started, ended_at=ended.isoformat(), sec=round(sec, 3), exit_code=rc,
                     ok=(None if rc is None else rc == 0), interrupted=interrupted, timing=timing,
                     session_id=ctx.session_id, task_id=ctx.task_id, agent=ctx.agent)
