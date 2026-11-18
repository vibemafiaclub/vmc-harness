"""하네스 소유 파일의 경로 지정 커밋 (spec 09 §저장 위치, 2026-11-18).

규칙 블록(AGENTS.md 표시 블록)·`.harness/rules.json`은 하네스가 바꾸는데 커밋하는 주체가 없어 작업 트리에 남았다
(설치 뒤 한 번도 커밋되지 않음, `git add -A` 한 번에 관계없는 커밋에 섞인다). 바꾼 경로만 하네스가 직접 커밋한다.

- 사람 index를 쓰지 않는다: 임시 index(GIT_INDEX_FILE)에 HEAD + 바뀐 경로만 올려 트리를 만들고 commit-tree,
  HEAD는 compare-and-swap(update-ref 새 옛)으로만 옮긴다. 그 사이 HEAD가 움직였으면 이번엔 건너뛴다
- 사람이 그 경로를 이미 staged 해 둔 경우(index ≠ HEAD)는 건드리지 않는다 — 다음 기회
- 커밋 뒤 사람 index의 그 경로만 새 내용으로 맞춘다(안 맞추면 `git status`가 옛 내용을 staged로 보인다). index.lock이면 재시도
- push하지 않는다(사람 커밋의 push를 따라간다 — 고객 레포 push 금지, AGENTS.md 금지 절). rebase·merge 중, detached HEAD면 건너뛴다
- 커밋 훅을 돌리지 않는다(commit-tree) — 하네스 메타 파일이라 훅 대상이 아니다
"""

import os
import tempfile
import time
from pathlib import Path

from vmc_harness.core import config
from vmc_harness.core.proc import run


def _git(root, args, env=None):
    e = None
    if env:
        e = dict(os.environ)
        e.update(env)
    return run(["git"] + args, cwd=str(root), timeout=config.get("gates.selfcommit_git_timeout_s", root), env=e)


def _busy(gitdir):
    g = Path(gitdir)
    return any((g / n).exists() for n in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD"))


def _retry(root, fn):
    n = int(config.get("gates.selfcommit_lock_retries", root))
    wait = float(config.get("gates.selfcommit_retry_ms", root)) / 1000
    for i in range(max(n, 1)):
        rc, out, err = fn()
        if rc == 0 or "index.lock" not in (err or ""):
            return rc, out, err
        time.sleep(wait)
    return rc, out, err


def commit_paths(root, rel_paths, subject, trailers=None):
    """→ {"ok": bool, "commit": sha|None, "paths": [...], "reason": str|None}. 바뀐 게 없으면 ok=True, commit=None."""
    res = {"ok": False, "commit": None, "paths": [], "reason": None}
    try:
        if not config.get("gates.selfcommit_enabled", root):
            res["reason"] = "disabled"
            return res
    except KeyError:
        return res
    rc, top, _ = _git(root, ["rev-parse", "--show-toplevel"])
    if rc != 0:
        res["reason"] = "not_git"
        return res
    top = Path(top.strip())
    rc, gitdir, _ = _git(root, ["rev-parse", "--absolute-git-dir"])
    if rc != 0 or _busy(gitdir.strip()):
        res["reason"] = "busy"
        return res
    rc, branch, _ = _git(root, ["symbolic-ref", "-q", "HEAD"])
    rc2, head, _ = _git(root, ["rev-parse", "-q", "--verify", "HEAD"])
    if rc != 0 or rc2 != 0:
        res["reason"] = "detached_or_empty"
        return res
    head = head.strip()
    entries = []
    for rel in rel_paths:
        p = Path(root) / rel
        if not p.is_file():
            continue
        r = os.path.relpath(str(p.resolve()), str(top.resolve())).replace(os.sep, "/")
        rc, blob, _ = _git(top, ["hash-object", "-w", "--", r])
        if rc != 0:
            continue
        blob = blob.strip()
        rc, hb, _ = _git(top, ["rev-parse", "-q", "--verify", "HEAD:" + r])
        hb = hb.strip() if rc == 0 else None
        if hb == blob:
            continue
        rc, ls, _ = _git(top, ["ls-files", "-s", "--", r])
        mode, ib = "100644", None
        if ls.strip():
            parts = ls.split()
            mode, ib = parts[0], parts[1]
        if ib is not None and ib != hb:
            res["reason"] = "staged_by_human"
            return res
        entries.append((mode, blob, r))
    if not entries:
        res["ok"] = True
        return res
    fd, tmp = tempfile.mkstemp(prefix="vmc-harness-index-")
    os.close(fd)
    os.unlink(tmp)
    env = {"GIT_INDEX_FILE": tmp}
    try:
        rc, _, err = _git(top, ["read-tree", head], env)
        if rc != 0:
            res["reason"] = "read_tree"
            return res
        for mode, blob, r in entries:
            _git(top, ["update-index", "--add", "--cacheinfo", "%s,%s,%s" % (mode, blob, r)], env)
        rc, tree, _ = _git(top, ["write-tree"], env)
        if rc != 0:
            res["reason"] = "write_tree"
            return res
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    msg = subject.strip() + "\n"
    tl = ["%s: %s" % (k, v) for k, v in (trailers or []) if v]
    if tl:
        msg += "\n" + "\n".join(tl) + "\n"
    rc, new, _ = run(["git", "commit-tree", tree.strip(), "-p", head, "-F", "-"], cwd=str(top),
                     timeout=config.get("gates.selfcommit_git_timeout_s", root), input_text=msg)
    if rc != 0:
        res["reason"] = "commit_tree"
        return res
    new = new.strip()
    rc, _, _ = _git(top, ["update-ref", "-m", "vmc-harness: " + subject.strip()[:60], "HEAD", new, head])
    if rc != 0:
        res["reason"] = "head_moved"
        return res
    for mode, blob, r in entries:
        rc, _, _ = _retry(top, lambda m=mode, b=blob, x=r: _git(top, ["update-index", "--add", "--cacheinfo", "%s,%s,%s" % (m, b, x)]))
        if rc != 0:
            res["reason"] = "index_stale"
    res.update(ok=True, commit=new, paths=[e[2] for e in entries])
    return res
