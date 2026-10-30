"""루프용 git 조작 — 별도 worktree·브랜치만 쓴다. 개발자 작업 트리·현재 브랜치는 건드리지 않는다(머지 단계 제외)."""

import datetime as _dt
import shutil
from pathlib import Path

from vmc_harness.core.clock import now
from vmc_harness.core.gitutil import git
from vmc_harness.core.project import local_dir

BRANCH_PREFIX = "vmc-cleanse/"
TRAILER_ACTION = "Harness-Action"


def main_branch(root):
    rc, out, _ = git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if rc == 0 and out.strip():
        name = out.strip().split("/", 1)[-1]
        if _branch_exists(root, name):
            return name
    for name in ("main", "master"):
        if _branch_exists(root, name):
            return name
    rc, out, _ = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return out.strip() if rc == 0 else "main"


def _branch_exists(root, name):
    rc, _, _ = git(root, "rev-parse", "--verify", "--quiet", "refs/heads/%s" % name)
    return rc == 0


def rev(root, ref):
    rc, out, _ = git(root, "rev-parse", ref)
    return out.strip() if rc == 0 else None


def worktrees_dir(root):
    return local_dir(root) / "worktrees"


def add_worktree(root, name, base):
    """(경로, 브랜치) 또는 (None, 이유)."""
    wt = worktrees_dir(root) / name
    branch = BRANCH_PREFIX + name
    remove_worktree(root, name)
    wt.parent.mkdir(parents=True, exist_ok=True)
    rc, _, err = git(root, "worktree", "add", "-q", "-b", branch, str(wt), base, timeout=120)
    if rc != 0:
        return None, (err or "").strip()[:200]
    for k, v in (("user.name", "vmc-harness"), ("user.email", "vmc-harness@local")):
        rc2, out2, _ = git(wt, "config", "--get", k)
        if rc2 != 0 or not out2.strip():
            git(wt, "config", k, v)
    return wt, branch


def remove_worktree(root, name, delete_branch=True):
    wt = worktrees_dir(root) / name
    if wt.exists():
        git(root, "worktree", "remove", "--force", str(wt), timeout=120)
        if wt.exists():
            shutil.rmtree(str(wt), ignore_errors=True)
    git(root, "worktree", "prune")
    if delete_branch:
        git(root, "branch", "-D", BRANCH_PREFIX + name)


def commit_all(wt, message):
    git(wt, "add", "-A")
    rc, out, _ = git(wt, "diff", "--cached", "--quiet")
    if rc == 0:
        return None  # 바뀐 것 없음
    rc, _, err = git(wt, "commit", "-q", "--no-verify", "-m", message)
    if rc != 0:
        return None
    return rev(wt, "HEAD")


def changed_files(wt, a, b):
    rc, out, _ = git(wt, "diff", "--name-only", a, b)
    return [l.strip() for l in out.splitlines() if l.strip()] if rc == 0 else []


def changed_lines(wt, a, b):
    """{rel: {"added": set(새 줄 번호), "removed": set(옛 줄 번호)}} — unified=0 diff 파싱."""
    import re
    rc, out, _ = git(wt, "diff", "-U0", "--no-color", a, b)
    res, cur = {}, None
    for line in out.splitlines() if rc == 0 else []:
        if line.startswith("+++ "):
            p = line[4:]
            cur = p[2:] if p.startswith("b/") else None
            if cur:
                res.setdefault(cur, {"added": set(), "removed": set()})
        elif line.startswith("--- "):
            continue
        elif line.startswith("@@") and cur:
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if not m:
                continue
            os_, oc = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            ns, nc = int(m.group(3)), int(m.group(4) if m.group(4) is not None else 1)
            res[cur]["removed"].update(range(os_, os_ + oc))
            res[cur]["added"].update(range(ns, ns + nc))
        elif line.startswith("--- a/"):
            continue
    # 삭제된 파일(+++ /dev/null)
    rc, out, _ = git(wt, "diff", "--name-status", a, b)
    for line in out.splitlines() if rc == 0 else []:
        parts = line.split("\t")
        if parts and parts[0].startswith("D") and len(parts) > 1:
            res.setdefault(parts[1], {"added": set(), "removed": set(), "deleted": True})["deleted"] = True
    return res


def diff_size(wt, a, b):
    rc, out, _ = git(wt, "diff", "--numstat", a, b)
    total = 0
    for line in out.splitlines() if rc == 0 else []:
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            total += int(parts[0]) + int(parts[1])
    return total


def rebase_onto(wt, onto):
    """(ok, 이유). 충돌이면 중단·원상복구하고 False."""
    rc, _, err = git(wt, "rebase", "-q", onto, timeout=300)
    if rc != 0:
        git(wt, "rebase", "--abort")
        return False, "충돌"
    return True, None


def is_ancestor(root, a, b):
    rc, _, _ = git(root, "merge-base", "--is-ancestor", a, b)
    return rc == 0


def human_commit_files(root, hours, ref=None):
    """최근 hours 시간 사람 커밋(하네스 조치 trailer 없는 것)이 건드린 파일."""
    since = (now() - _dt.timedelta(hours=hours)).isoformat(timespec="seconds")
    args = ["log", "--since", since, "--name-only", "--format=%x00%H%x01%B%x02"]
    if ref:
        args.append(ref)
    rc, out, _ = git(root, *args, timeout=60)
    return _files_from_log(out, rc)


def _files_from_log(out, rc):
    files = set()
    if rc != 0:
        return files
    for chunk in out.split("\0"):
        if not chunk.strip():
            continue
        head, _, names = chunk.partition("\x02")
        body = head.partition("\x01")[2]
        if TRAILER_ACTION + ":" in body:
            continue
        files.update(l.strip() for l in names.splitlines() if l.strip())
    return files


def human_commit_count(root, path, since_commit, ref):
    """since_commit 이후 ref까지 path를 건드린 사람 커밋 수."""
    if not since_commit:
        return 0
    rc, out, _ = git(root, "log", "--format=%x00%H%x01%B%x02", "%s..%s" % (since_commit, ref), "--", path, timeout=60)
    if rc != 0:
        return 0
    n = 0
    for chunk in out.split("\0"):
        if chunk.strip() and TRAILER_ACTION + ":" not in chunk.partition("\x01")[2]:
            n += 1
    return n


def open_branch_files(root, main):
    """main 말고 열린 로컬 브랜치(루프 브랜치 제외)가 main 대비 건드린 파일."""
    rc, out, _ = git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    files = set()
    for b in out.splitlines() if rc == 0 else []:
        b = b.strip()
        if not b or b == main or b.startswith(BRANCH_PREFIX):
            continue
        rc2, out2, _ = git(root, "diff", "--name-only", "%s...%s" % (main, b), timeout=60)
        if rc2 == 0:
            files.update(l.strip() for l in out2.splitlines() if l.strip())
    return files


def dirty_files(root):
    rc, out, _ = git(root, "status", "--porcelain")
    files = set()
    for line in out.splitlines() if rc == 0 else []:
        p = line[3:].strip()
        if " -> " in p:
            p = p.split(" -> ", 1)[1]
        files.add(p.strip('"'))
    return files


def current_branch(root):
    rc, out, _ = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    return out.strip() if rc == 0 else None


def fast_forward_main(root, main, branch):
    """main을 branch로 빨리감기. 개발자 작업 트리를 덮지 않는다. (ok, 이유)"""
    tip = rev(root, branch)
    base = rev(root, main)
    if not tip or not base:
        return False, "브랜치 없음"
    if not is_ancestor(root, base, tip):
        return False, "main이 움직임"
    if current_branch(root) == main:
        rc, _, err = git(root, "merge", "--ff-only", "-q", branch, timeout=120)
        if rc != 0:
            return False, "작업 트리 변경과 겹침"
        return True, None
    rc, _, err = git(root, "update-ref", "refs/heads/%s" % main, tip, base)
    return (rc == 0), (None if rc == 0 else "ref 갱신 실패")


def path_exists_in(wt, rel):
    return (Path(wt) / rel).exists()
