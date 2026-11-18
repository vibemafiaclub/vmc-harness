"""`.harness/.gitignore` 관리 (spec 09 §저장 위치). init과 세션 시작(이미 설치된 프로젝트의 1회 마이그레이션)이 부른다.

- 모든 프로파일: `cache/`(다시 만들 수 있는 것)
- `bootstrap.ledger_git_local`(solo 기본): `ledger/`·`lexicon.json`도 — 팀 합산이 없으면 장부는 이 PC 로컬 원본이다
- 이미 git이 추적 중인 파일은 건드리지 않는다(`git rm --cached`는 사람 작업 트리의 index를 바꾸므로 하네스가 하지 않는다).
  `pending_untrack(root)`가 그 목록을 돌려주고, 꼭 알릴 일 한 번으로 남긴다
"""

from pathlib import Path

from vmc_harness.core import config, paths

BASE = ["cache/"]
LOCAL = ["ledger/", "lexicon.json"]


def wanted(root):
    out = list(BASE)
    try:
        if config.get("bootstrap.ledger_git_local", root):
            out += LOCAL
    except KeyError:
        pass
    return out


def ensure(root):
    """빠진 줄만 덧붙인다(사람이 쓴 줄은 그대로). → 새로 넣은 줄 목록."""
    hd = paths.harness_dir(root)
    if not hd.is_dir():
        return []
    gi = hd / ".gitignore"
    text = gi.read_text(encoding="utf-8") if gi.is_file() else ""
    have = {ln.strip() for ln in text.splitlines()}
    add = [w for w in wanted(root) if w not in have]
    if add:
        sep = "" if not text or text.endswith("\n") else "\n"
        with gi.open("a", encoding="utf-8", newline="\n") as f:
            f.write(sep + "".join(a + "\n" for a in add))
    return add


def pending_untrack(root):
    """무시 목록에 있는데 git이 아직 추적 중인 `.harness/` 아래 경로(레포 기준 상대). git이 아니면 빈 목록."""
    from vmc_harness.core.proc import run
    rel = [".harness/" + w.rstrip("/") for w in wanted(root) if w != "cache/"] + [".harness/cache"]
    rc, out, _ = run(["git", "ls-files", "-z", "--"] + rel, cwd=str(root), timeout=config.get("bootstrap.command_timeout_s", root))
    if rc != 0:
        return []
    return sorted({p for p in out.split("\0") if p})


def migrate(root):
    """세션 시작 1회: 무시 줄 보충 + 추적 중인 로컬 파일이 있으면 꼭 알릴 일 한 번(같은 내용은 다시 안 냄).
    → {"added": [...], "tracked": n}"""
    added = ensure(root)
    tracked = pending_untrack(root)
    if tracked:
        from vmc_harness.core import notices
        from vmc_harness.core.jsonio import read_json, write_json
        from vmc_harness.core.project import local_dir
        sp = local_dir(root) / "gitignore-state.json"
        st = read_json(sp, {}) or {}
        key = "%d" % len(tracked)
        if st.get("noticed") != key:
            dirs = sorted({"/".join(p.split("/")[:2]) for p in tracked})
            notices.add(root, "ledger_tracked", "must",
                        "하네스 로컬 장부가 git에 추적되고 있어요(%d개) — `git rm -r --cached %s` 한 번이면 이후 커밋에 섞이지 않아요"
                        % (len(tracked), " ".join(dirs)), refs={"paths": dirs})
            st["noticed"] = key
            write_json(sp, st)
    return {"added": added, "tracked": len(tracked)}
