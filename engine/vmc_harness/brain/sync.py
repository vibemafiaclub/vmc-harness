"""브레인 원격 연동 (spec 04 — 선택). 연동 안 하면 로컬 git 커밋까지만.

- link(url): origin 추가 → 원격에 카드가 있으면 `--allow-unrelated-histories`로 합친 뒤 push(카드 1장 = 파일 1개라 충돌 없음)
- pull(): 세션 시작 때 `git pull --ff-only`(상한 brain.pull_timeout_s, 실패하면 로컬 사본으로). 앞서 있으면 push
- background(): 훅이 기다리지 않게 분리 실행(`vmc-harness link-brain --sync`)
"""

from vmc_harness.brain import store
from vmc_harness.core import config
from vmc_harness.core.proc import run


def _git(*args, timeout=None):
    return run(["git"] + store._ident_args() + list(args), cwd=str(store.brain_dir()), timeout=timeout)


def remote_url():
    if not (store.brain_dir() / ".git").exists():
        return None
    rc, out, _ = store.git_b("remote", "get-url", "origin", timeout=5)
    return out.strip() if rc == 0 and out.strip() else None


def _current_branch():
    rc, out, _ = store.git_b("rev-parse", "--abbrev-ref", "HEAD", timeout=5)
    return out.strip() if rc == 0 else "main"


def _remote_default_branch(timeout):
    rc, out, _ = store.git_b("ls-remote", "--symref", "origin", "HEAD", timeout=timeout)
    if rc == 0:
        for line in out.splitlines():
            if line.startswith("ref:") and "refs/heads/" in line:
                return line.split("refs/heads/", 1)[1].split()[0]
    rc, out, _ = store.git_b("ls-remote", "--heads", "origin", timeout=timeout)
    heads = [l.split("refs/heads/", 1)[1] for l in out.splitlines() if "refs/heads/" in l] if rc == 0 else []
    if not heads:
        return None
    return "main" if "main" in heads else heads[0]


def link(url):
    """원격 연결. 반환 dict(merged, pushed, branch, error)."""
    store.ensure_repo()
    timeout = config.get("brain.link_timeout_s")
    if remote_url():
        store.git_b("remote", "set-url", "origin", url)
    else:
        store.git_b("remote", "add", "origin", url)
    res = {"url": url, "merged": False, "pushed": False}
    remote_branch = _remote_default_branch(timeout)
    local = _current_branch()
    if remote_branch:
        rc, _, err = _git("fetch", "origin", remote_branch, timeout=timeout)
        if rc != 0:
            res["error"] = "fetch 실패: %s" % err.strip()[-200:]
            return res
        if local != remote_branch:
            store.git_b("branch", "-M", remote_branch)
            local = remote_branch
        rc, _, err = _git("merge", "--no-edit", "--allow-unrelated-histories",
                          "-m", "brain: 원격 카드와 합침", "origin/%s" % remote_branch, timeout=timeout)
        if rc != 0:
            _git("merge", "--abort")
            res["error"] = "합치기 실패: %s" % err.strip()[-200:]
            return res
        res["merged"] = True
    branch = remote_branch or local
    rc, _, err = _git("push", "-u", "origin", "%s:%s" % (local, branch), timeout=timeout)
    res["pushed"] = rc == 0
    res["branch"] = branch
    if rc != 0:
        res["error"] = "push 실패: %s" % err.strip()[-200:]
    return res


def pull():
    """세션 시작 동기화. 원격 없으면 아무것도 안 한다. 실패해도 로컬 사본으로 계속."""
    if not remote_url():
        return {"remote": False}
    timeout = config.get("brain.pull_timeout_s")
    rc, _, err = _git("pull", "--ff-only", timeout=timeout)
    res = {"remote": True, "pulled": rc == 0}
    if rc != 0 and rc != -9:
        # 양쪽에 새 카드가 있으면 ff가 안 된다 — 카드 1장 = 파일 1개라 합치기로 충돌 없이 받는다(충돌이면 취소)
        rc2, _, err2 = _git("pull", "--no-rebase", "--no-edit", timeout=timeout)
        if rc2 == 0:
            res.update(pulled=True, merged=True)
        else:
            _git("merge", "--abort")
            res["error"] = err2.strip()[-200:]
    elif rc == -9:
        res["error"] = "timeout"
    rc, out, _ = store.git_b("rev-list", "--count", "@{u}..HEAD", timeout=5)
    if rc == 0 and out.strip().isdigit() and int(out.strip()) > 0:
        rc, _, _ = _git("push", timeout=timeout)
        res["pushed"] = rc == 0
    return res


def background():
    """훅에서 부른다 — 분리 실행만 하고 바로 돌아온다."""
    if not remote_url():
        return False
    from vmc_harness.ledger.handlers import spawn_cli
    try:
        spawn_cli(["link-brain", "--sync"])
        return True
    except OSError:
        return False
