"""사람의 git revert 감지 → finding(kind=revert) (spec 01 P2, 04).

세션 시작 판정 때 최근 커밋을 훑어 `This reverts commit X`가 하네스 조치(Harness-Action)·작업(Harness-Task)
trailer 커밋을 되돌린 것을 찾는다. 하네스 자신이 만든 revert(`Harness-Reverted-By: harness`)는 건너뛴다.
같은 revert 커밋은 팀 전체에서 finding 하나 — 장부의 기존 finding(target.revert_commit)로 중복을 막는다.
"""

import re
from pathlib import Path

from vmc_harness.core import config
from vmc_harness.core import ledger as core_ledger
from vmc_harness.core.gitutil import git
from vmc_harness.core.proc import no_window
from vmc_harness.ledger.revert import HARNESS_REVERT_TRAILER, LEDGER_PATH

REVERTS_RE = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
ACTION_RE = re.compile(r"^Harness-Action:\s*(A-[\w-]+)\s*$", re.M)
TASK_RE = re.compile(r"^Harness-Task:\s*(T-[\w-]+)\s*$", re.M)


def _log(root, n):
    rc, out, _ = git(root, "log", "-n", str(n), "--format=%H%x00%B%x1e")
    if rc != 0:
        return []
    rows = []
    for chunk in out.split("\x1e"):
        chunk = chunk.strip("\n")
        if "\x00" in chunk:
            sha, body = chunk.split("\x00", 1)
            rows.append((sha.strip(), body))
    return rows


def _body(root, sha):
    rc, out, _ = git(root, "log", "-1", "--format=%B", sha)
    return out if rc == 0 else ""


def _blob(root, rev, path):
    import subprocess
    r = subprocess.run(["git", "show", "%s:%s" % (rev, path)], cwd=str(root), capture_output=True,
                       **no_window())
    return r.stdout if r.returncode == 0 else None


def restore_ledger(root, revert_sha):
    """사람의 git revert가 커밋에 실려 있던 장부 기록(.harness/ledger/)까지 되돌렸으면 작업 트리에 되살린다.
    그 뒤 아무도 안 바꾼 파일만(지금 내용 = revert 직후 내용). 되살린 파일은 다음 개발 커밋에 실린다."""
    rc, out, _ = git(root, "diff", "--name-only", revert_sha + "^1", revert_sha, "--", LEDGER_PATH)
    restored = []
    if rc != 0:
        return restored
    for rel in [l.strip() for l in out.splitlines() if l.strip()]:
        p = Path(root) / rel
        now_bytes = p.read_bytes() if p.is_file() else None
        if now_bytes != _blob(root, revert_sha, rel):
            continue
        before = _blob(root, revert_sha + "^1", rel)
        if before is None:
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(before)
        restored.append(rel)
    return restored


def scan(root, n=None):
    """새 revert finding 목록."""
    n = n or config.get("judge.revert_scan_commits", root)
    seen = {((f.get("target") or {}).get("revert_commit")) for f in core_ledger.list_findings(root)}
    made = []
    for sha, body in _log(root, n):
        if HARNESS_REVERT_TRAILER in body or sha in seen:
            continue
        for reverted in REVERTS_RE.findall(body):
            orig = _body(root, reverted)
            actions = ACTION_RE.findall(orig)
            tasks = TASK_RE.findall(orig)
            if not actions and not tasks:
                continue
            restore_ledger(root, sha)
            target = {"commit": reverted, "revert_commit": sha}
            if actions:
                target["action_id"] = actions[0]
                if len(actions) > 1:
                    target["action_ids"] = actions
            if tasks:
                target["task_id"] = tasks[0]
            made.append(core_ledger.create_finding(root, "revert", target, source_event="git_revert"))
            seen.add(sha)
            break
    return made
