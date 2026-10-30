"""원복 (spec 04 §판정 — 원복 = 해당 커밋 git revert + 장부 상태 변경, 커널은 원복 대상 아님).

- 조치 커밋 = action.commits ∪ `Harness-Action: <id>` trailer 커밋
- 하네스가 만든 revert 커밋에는 `Harness-Reverted-By: harness` trailer — 사람 revert 감지(git_watch)가 이걸 건너뛴다
- 커밋이 없는 L1 조치(개발 커밋에 실려 아직 안 올라간 규칙·게이트·카드)는 그 파일의 상태만 바꾼다
- 스테이징된 사람 변경이 있으면 섞지 않으려고 미룬다(다음 판정 때 다시) — 사람 작업을 건드리지 않는다
- by_human=True(사람이 "원복해"·`revert` 명령)면 finding(kind=revert)을 남긴다. 판정(judge)이 부르면 finding 없음
"""

import re

from vmc_harness.core import ledger as core_ledger
from vmc_harness.core import notices, paths
from vmc_harness.core.clock import now_iso
from vmc_harness.core.gitutil import git
from vmc_harness.core.proc import no_window
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.project import local_dir

HARNESS_REVERT_TRAILER = "Harness-Reverted-By: harness"
LEDGER_PATH = ".harness/ledger"
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


class RevertDeferred(RuntimeError):
    """지금은 원복할 수 없다(스테이징된 사람 변경·충돌). 다음 판정 때 다시."""


# ---- git ----

def commits_with_trailer(root, key, value):
    rc, out, _ = git(root, "log", "--format=%H", "-F", "--grep", "%s: %s" % (key, value))
    return [l.strip() for l in out.splitlines() if l.strip()] if rc == 0 else []


def already_reverted(root, sha):
    rc, out, _ = git(root, "log", "--format=%H", "-F", "--grep", "This reverts commit %s" % sha)
    return rc == 0 and bool(out.strip())


def _full_sha(root, sha):
    rc, out, _ = git(root, "rev-parse", "--verify", "--quiet", sha + "^{commit}")
    return out.strip() if rc == 0 and out.strip() else None


def _is_ancestor(root, sha):
    rc, _, _ = git(root, "merge-base", "--is-ancestor", sha, "HEAD")
    return rc == 0


def _is_merge(root, sha):
    rc, out, _ = git(root, "rev-list", "--parents", "-n", "1", sha)
    return rc == 0 and len(out.split()) > 2


def _staged_changes(root):
    rc, _, _ = git(root, "diff", "--cached", "--quiet")
    return rc == 1


def git_revert(root, shas, trailers):
    """shas를 최신부터 되돌린다. 성공한 revert 커밋 목록. 실패하면 되돌린 것까지만 남기고 RevertDeferred."""
    if _staged_changes(root):
        raise RevertDeferred("스테이징된 변경이 있어 원복을 미룸")
    full = []
    for s in shas:
        f = _full_sha(root, s)
        if f and _is_ancestor(root, f) and not already_reverted(root, f) and f not in full:
            full.append(f)
    # 최신 커밋부터(의존 순서)
    rc, out, _ = git(root, "rev-list", "HEAD")
    order = {sha: i for i, sha in enumerate(out.split())} if rc == 0 else {}
    full.sort(key=lambda s: order.get(s, 0))
    made = []
    for f in full:
        sha = _revert_one(root, f, trailers)
        if sha:
            made.append(sha)
    return made


def _revert_one(root, sha, trailers):
    """`git revert`와 같은 결과를 만들되 장부(.harness/ledger/)는 건드리지 않는다.
    장부 기록은 개발 커밋에 함께 실리므로 커밋째 되돌리면 조치·finding 기록까지 사라진다."""
    import os
    import subprocess
    import tempfile
    parent = sha + "^1"
    excl = ":(exclude)%s" % LEDGER_PATH
    r = subprocess.run(["git", "diff", "--binary", sha, parent, "--", ".", excl], cwd=str(root), capture_output=True,
                       **no_window())
    if r.returncode != 0:
        raise RevertDeferred("diff 실패")
    if not r.stdout.strip():
        return None
    fd, tmp = tempfile.mkstemp(prefix="vmh-revert-", suffix=".patch")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(r.stdout)
        rc, _, err = git(root, "apply", "--index", "--whitespace=nowarn", tmp)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    if rc != 0:  # git apply는 전부 아니면 전무 — 남는 변경 없음
        raise RevertDeferred("되돌릴 수 없음(이후 변경과 겹침): %s" % (err.strip().splitlines() or ["?"])[-1])
    subj = git(root, "log", "-1", "--format=%s", sha)[1].strip()
    body = 'Revert "%s"\n\nThis reverts commit %s.\n\n하네스 변경\n%s\n' % (
        subj, sha, "\n".join(trailers + [HARNESS_REVERT_TRAILER]))
    rc, _, err = git(root, "commit", "--no-verify", "-q", "-m", body)
    if rc != 0:
        git(root, "reset", "-q", "--merge")
        raise RevertDeferred("원복 커밋 실패: %s" % err.strip()[-200:])
    return git(root, "rev-parse", "HEAD")[1].strip()


# ---- L1 파일 상태(커밋 없는 조치) ----

def _rules_file(root):
    return paths.harness_dir(root) / "rules.json"


def set_rule_status(root, rule_id, status):
    p = _rules_file(root)
    data = read_json(p, None)
    if data is None:
        return False
    rules = data.get("rules", []) if isinstance(data, dict) else data
    hit = False
    for r in rules:
        if isinstance(r, dict) and (r.get("id") or r.get("rule_id")) == rule_id:
            r["status"] = status
            r["enabled"] = status not in ("reverted", "disabled")  # gates.rules 형식(enabled)과 맞춘다
            r["status_at"] = now_iso()
            hit = True
    if hit:
        write_json(p, data)
    return hit


def set_gate_status(root, gate_id, status):
    p = paths.harness_dir(root) / "gates" / ("%s.json" % gate_id)
    data = read_json(p, None)
    if not isinstance(data, dict):
        return False
    if data.get("status") == status and not (status in ("reverted", "disabled") and data.get("enabled", True)):
        return True  # 이미 같은 상태 — 파일을 다시 쓰지 않는다
    data["status"] = status
    data["status_at"] = now_iso()
    if status in ("reverted", "disabled"):
        data["enabled"] = False  # 원복된 게이트가 enabled true로 남지 않게(표기와 동작을 맞춘다)
        data.setdefault("disabled_reason", "reverted")
    write_json(p, data)
    return True


def _unapply_layer(root, action, status="reverted"):
    t = action.get("target") or {}
    done = []
    if t.get("card"):
        try:
            from vmc_harness.brain import store
            if store.set_status(t["card"], status, reason="action %s" % action["action_id"]):
                done.append("card")
        except (ImportError, KeyError):
            pass
    if t.get("rule_id") and set_rule_status(root, t["rule_id"], status):
        done.append("rule")
        try:
            from vmc_harness.gates import rules
            rules.render(root)   # AGENTS.md 표시 블록도 바로 갱신(다음 세션부터 그 규칙이 읽히지 않게)
        except Exception:
            pass
    if t.get("gate_id") and set_gate_status(root, t["gate_id"], status):
        done.append("gate")
    return done


# ---- 미룬 원복 ----

def _pending_file(root):
    return local_dir(root) / "pending-reverts.json"


def _defer(root, action_id, reason, by_human, why):
    p = _pending_file(root)
    rows = [r for r in (read_json(p, []) or []) if r.get("action_id") != action_id]
    rows.append({"action_id": action_id, "reason": reason, "by_human": by_human, "deferred": why, "at": now_iso()})
    write_json(p, rows)


def retry_pending(root):
    p = _pending_file(root)
    rows = read_json(p, []) or []
    if not rows:
        return []
    write_json(p, [])
    done = []
    for r in rows:
        res = revert_action(root, r["action_id"], r.get("reason") or "", by_human=bool(r.get("by_human")),
                            make_finding=False)
        if res and res.get("status") == "reverted":
            done.append(r["action_id"])
    return done


# ---- 원복 ----

def action_commits(root, action):
    shas = list(action.get("commits") or [])
    for s in commits_with_trailer(root, "Harness-Action", action["action_id"]):
        if s not in shas:
            shas.append(s)
    return shas


def revert_action(root, action_id, reason, by_human=True, make_finding=True):
    """조치 원복. by_human이면 finding(revert)도. 반환: 갱신된 action 기록(미루면 status 그대로 + deferred)."""
    a = core_ledger.get_action(root, action_id)
    if a is None:
        raise KeyError(action_id)
    if a.get("status") == "reverted":
        return a
    if a.get("kernel"):
        raise ValueError("커널은 원복 대상이 아님")
    if by_human and make_finding:
        core_ledger.create_finding(root, "revert", {"action_id": action_id})
    try:
        made = git_revert(root, action_commits(root, a),
                          ["Harness-Action: %s" % action_id])
    except RevertDeferred as e:
        _defer(root, action_id, reason, by_human, str(e))
        a["deferred"] = str(e)
        return a
    layers = _unapply_layer(root, a)
    rec = core_ledger.update_action(root, action_id, status="reverted", reverted_at=now_iso(),
                                    revert_reason=reason, revert_commits=made,
                                    reverted_by="human" if by_human else "judge", unapplied=layers)
    t = a.get("target") or {}
    notices.add(root, "action_reverted", "must",
                "하네스 조치 원복: %s — %s" % (a.get("hypothesis") or action_id, reason),
                area=t.get("area"), refs={"action_id": action_id, "commits": made})
    return rec


def revert_task(root, task_id, reason, by_human=True):
    """사람이 "그 작업 원복해" — 그 작업 커밋(Harness-Task trailer)을 되돌리고 finding 1건.
    그 커밋에 실린 하네스 조치(Harness-Action trailer)도 상태를 원복으로."""
    shas = commits_with_trailer(root, "Harness-Task", task_id)
    if not shas:
        raise KeyError("작업 커밋 없음: %s" % task_id)
    if by_human:
        core_ledger.create_finding(root, "revert", {"commit": shas[0], "task_id": task_id})
    actions = set()
    for s in shas:
        rc, body, _ = git(root, "log", "-1", "--format=%B", s)
        actions.update(re.findall(r"^Harness-Action:\s*(A-[\w-]+)\s*$", body, re.M))
    made = git_revert(root, shas, ["Harness-Task: %s" % task_id])
    for aid in sorted(actions):
        a = core_ledger.get_action(root, aid)
        if a and a.get("status") != "reverted":
            _unapply_layer(root, a)
            core_ledger.update_action(root, aid, status="reverted", reverted_at=now_iso(),
                                      revert_reason=reason, reverted_by="human" if by_human else "judge")
    return {"task_id": task_id, "reverted_commits": shas, "revert_commits": made, "actions": sorted(actions)}


def revert_commit(root, sha, reason, by_human=True, finding_kind="revert"):
    full = _full_sha(root, sha)
    if not full:
        raise KeyError(sha)
    if by_human:
        core_ledger.create_finding(root, finding_kind, {"commit": full})
    return {"commit": full, "revert_commits": git_revert(root, [full], [])}


def revert(root, ident, reason="사람 요청", by_human=True):
    """CLI 진입: A-… 조치 / T-… 작업 / 커밋 해시."""
    if ident.startswith("A-"):
        return revert_action(root, ident, reason, by_human=by_human)
    if ident.startswith("T-"):
        return revert_task(root, ident, reason, by_human=by_human)
    if SHA_RE.match(ident):
        return revert_commit(root, ident, reason, by_human=by_human)
    raise KeyError(ident)
