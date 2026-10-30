"""커밋 trailer (spec 03 §커밋 trailer, 09 원칙 2 — "하네스가 바꾼 파일"은 git trailer에서 뽑는다).

- 모든 변경 커밋: `Harness-Task: <task_id>`
- 하네스 조치 커밋(L1 변경 포함): `Harness-Action: <action_id>`
- 요청 밖 변경이 이 커밋에 있으면: `Harness-OutOfScope: <파일 목록>`
- 하네스 L1 변경(`.harness/` 중 ledger·cache 밖, AGENTS.md 표시 블록)은 "하네스 변경: …" 줄을 따로
- 하네스는 push하지 않는다(사람 개입 기준 1) — 여기엔 push가 없다

git `prepare-commit-msg` 훅으로 설치: install_commit_hook(root) (init이 부른다). 기존 훅이 있으면 이어 부른다.
"""

import os
import stat
import sys
from pathlib import Path

from vmc_harness.core import gitutil
from vmc_harness.task import scope, state as st

KEYS = ("Harness-Task", "Harness-Action", "Harness-OutOfScope")
HARNESS_LINE = "하네스 변경"
HOOK_MARK = "# vmc-harness prepare-commit-msg"
L1_EXCLUDE = (".harness/ledger/", ".harness/cache/")
SKIP_SOURCES = ("merge", "squash")


def _staged(root):
    rc, out, _ = gitutil.git(root, "diff", "--cached", "--name-only")
    return [scope.norm(l) for l in out.splitlines() if l.strip()] if rc == 0 else []


def _l1_files(root, staged):
    out = [f for f in staged if f.startswith(scope.HARNESS_PREFIX) and not f.startswith(L1_EXCLUDE)]
    if "AGENTS.md" in staged:
        rc, diff, _ = gitutil.git(root, "diff", "--cached", "-U0", "--", "AGENTS.md")
        if rc == 0 and "vmc-harness" in diff:
            out.append("AGENTS.md")
    return out


def task_for_commit(root, session_id=None):
    """세션 환경변수가 있으면 그 세션 작업, 없으면 진행 중 작업이 하나뿐일 때만 그 작업."""
    sid = session_id
    if not sid:
        for k in st.SESSION_ENV:
            if os.environ.get(k):
                sid = os.environ[k]
                break
    if sid:
        t = st.current(root, sid)
        if t:
            return t
    tasks = st.open_tasks(root)
    return tasks[0] if len(tasks) == 1 else None


def _actions(root, task):
    ids = list(task.get("actions") or [])
    try:
        from vmc_harness.core import ledger
        for a in ledger.list_actions(root):
            if a and a.get("task_id") == task["task_id"] and a.get("action_id") not in ids:
                ids.append(a["action_id"])
    except Exception:
        pass
    return ids


def lines_for_commit(root, task=None, staged=None):
    """붙일 줄 목록(trailer + "하네스 변경" 줄). 작업이 없으면 L1 줄만."""
    staged = staged if staged is not None else _staged(root)
    trailers, extra = [], []
    l1 = _l1_files(root, staged)
    if task:
        trailers.append("Harness-Task: %s" % task["task_id"])
        if l1:
            for aid in _actions(root, task):
                trailers.append("Harness-Action: %s" % aid)
        oos = [o["path"] for o in scope.out_of_scope(root, task) if o["path"] in staged]
        if oos:
            trailers.append("Harness-OutOfScope: %s" % ", ".join(oos))
    if l1:
        extra.append("%s: %s" % (HARNESS_LINE, ", ".join(l1)))
    return extra, trailers


def apply(message, extra, trailers):
    """메시지 끝에 붙인다. 이미 있는 줄은 다시 붙이지 않는다. 주석(#) 줄 앞에 넣는다."""
    lines = message.splitlines()
    body, comments = [], []
    for i, l in enumerate(lines):
        if l.startswith("#") and all(x.startswith("#") or not x.strip() for x in lines[i:]):
            comments = lines[i:]
            break
        body.append(l)
    while body and not body[-1].strip():
        body.pop()
    have = set(x.strip() for x in body)
    add_extra = [x for x in extra if x not in have]
    add_tr = [x for x in trailers if x not in have]
    if not add_extra and not add_tr:
        return message
    out = list(body)
    if add_extra:
        out += ([""] if out else []) + add_extra
    if add_tr:
        last_is_trailer = bool(out) and not add_extra and any(
            out[-1].startswith(k + ":") for k in KEYS + ("Co-Authored-By", "Signed-off-by"))
        if out and not last_is_trailer:
            out.append("")
        out += add_tr
    text = "\n".join(out) + "\n"
    if comments:
        text += "\n" + "\n".join(comments) + "\n"
    return text


def prepare_commit_msg(root, msg_file, source=None):
    """git prepare-commit-msg에서 부른다. 실패해도 커밋을 막지 않는다."""
    if source in SKIP_SOURCES:
        return False
    p = Path(msg_file)
    try:
        msg = p.read_text(encoding="utf-8")
    except OSError:
        return False
    extra, trailers = lines_for_commit(root, task_for_commit(root))
    new = apply(msg, extra, trailers)
    if new != msg:
        with p.open("w", encoding="utf-8", newline="\n") as f:
            f.write(new)
        return True
    return False


# ---- git 훅 설치 ----

def _hooks_dir(root):
    rc, out, _ = gitutil.git(root, "rev-parse", "--git-path", "hooks")
    if rc != 0 or not out.strip():
        return None
    d = Path(out.strip())
    return d if d.is_absolute() else Path(root) / d


def _sh_quote(s):
    return "'" + str(s).replace("\\", "/").replace("'", "'\\''") + "'"


def _invocation():
    """훅 sh가 엔진을 부르는 줄들. PATH의 `vmc-harness`에 기대지 않는다 — Windows 런처는 .cmd라 sh가 못 돌리고,
    훅 환경 PATH에 python이 `python3` 이름으로 없을 수 있다. 지금 도는 파이썬 절대 경로로 부르고, 그게 없어졌을 때만 PATH에서 찾는다.
    설치본(커널 폴더 안에서 도는 엔진)은 껍데기(vmc_harness_shell.py)를 불러 업데이트 뒤에도 현재 커널을 쓰고,
    개발 체크아웃은 PYTHONPATH(구분자는 OS 것 — Windows ';')로 이 엔진을 직접 부른다."""
    lines = [
        "VMH_PY=%s" % _sh_quote(sys.executable),
        '[ -x "$VMH_PY" ] || VMH_PY="$(command -v python3 || command -v python || command -v py)"',
    ]
    engine = Path(__file__).resolve().parent.parent.parent
    try:
        from vmc_harness.dist import shell
        installed = shell.shell_path().is_file() and shell.kernel_root().resolve() in engine.parents
    except Exception:
        installed = False
    if installed:
        lines.append('"$VMH_PY" %s commit-msg "$@" >/dev/null 2>&1 || true' % _sh_quote(shell.shell_path()))
    else:
        lines.append('PYTHONPATH=%s"${PYTHONPATH:+%s$PYTHONPATH}" "$VMH_PY" -m vmc_harness commit-msg "$@" >/dev/null 2>&1 || true'
                     % (_sh_quote(engine), os.pathsep))
    return lines


def install_commit_hook(root):
    """`.git/hooks/prepare-commit-msg` 설치(sh — Git for Windows도 sh로 훅을 돈다). 기존 훅은 .local로 옮겨 이어 부른다."""
    d = _hooks_dir(root)
    if d is None:
        return None
    d.mkdir(parents=True, exist_ok=True)
    hook = d / "prepare-commit-msg"
    local = d / "prepare-commit-msg.local"
    if hook.is_file() and HOOK_MARK not in hook.read_text(encoding="utf-8", errors="replace"):
        if not local.exists():
            hook.replace(local)
    script = "\n".join([
        "#!/bin/sh",
        HOOK_MARK,
        'if [ -x "$(dirname "$0")/prepare-commit-msg.local" ]; then',
        '  "$(dirname "$0")/prepare-commit-msg.local" "$@" || exit $?',
        "fi",
    ] + _invocation() + [
        "exit 0",
        "",
    ])
    with hook.open("w", encoding="utf-8", newline="\n") as f:  # Windows에서도 LF(sh가 읽는다)
        f.write(script)
    try:
        hook.chmod(hook.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass
    return hook


def uninstall_commit_hook(root):
    d = _hooks_dir(root)
    if d is None:
        return False
    hook = d / "prepare-commit-msg"
    local = d / "prepare-commit-msg.local"
    if hook.is_file() and HOOK_MARK in hook.read_text(encoding="utf-8", errors="replace"):
        hook.unlink()
        if local.is_file():
            local.replace(hook)
        return True
    return False
