"""`vmc-harness remove` (spec 10) — 훅·MCP·스케줄러·표시 블록·commit hook 원상복구, 로컬 원본 삭제.

- 각 영역 unregister 함수를 부른다(없으면 건너뜀)
- init이 만든 CLAUDE.md(`@AGENTS.md` 한 줄 그대로)·빈 AGENTS.md는 지운다. 고객이 손댄 파일은 남긴다
- `~/.vmc-harness/projects/<project_id>/` 삭제
- `.harness/`는 팀 장부라 기본 남김. `purge_harness=True`면 지운다
- Claude Code cleanupPeriodDays는 되돌리지 않는다(보관 기간을 늘린 것뿐이라 해가 없다)
"""

import shutil
from pathlib import Path

from vmc_harness.bootstrap import util
from vmc_harness.core import paths
from vmc_harness.core.jsonio import read_json

BEGIN = "<!-- vmc-harness:begin -->"
END = "<!-- vmc-harness:end -->"


def _strip_block(root):
    st, res = util.optional_call([("vmc_harness.gates.rules", "unrender")], str(root))
    if st == "ok":
        return bool(res)
    from vmc_harness.core import textfile
    p = Path(root) / "AGENTS.md"
    t, fmt = textfile.read(p)
    if t is None:
        return False
    if BEGIN in t and END in t:
        n = t[:t.index(BEGIN)].rstrip("\r\n") + fmt["eol"] + t[t.index(END) + len(END):].lstrip("\r\n")
        textfile.write(p, n if n.strip() else "", fmt)
        return True
    return False


def _strip_claude_import(root, cfg):
    """init이 기존 CLAUDE.md 끝에 덧붙인 `@AGENTS.md` 한 줄만 걷는다(줄끝·BOM 유지)."""
    if not cfg.get("claude_import_added_at"):
        return False
    from vmc_harness.core import textfile
    from vmc_harness.gates import rules as grules
    p = Path(root) / "CLAUDE.md"
    t, fmt = textfile.read(p)
    if t is None:
        return False
    n = grules.remove_claude_import(t)
    if n != t:
        textfile.write(p, n, fmt)
        return True
    return False


def _mod_unregister(root):
    """mod를 등록한 기록이 있을 때만 `claude plugin uninstall`을 부른다(등록 안 한 PC의 사용자 설정을 건드리지 않게)."""
    from vmc_harness.render import mod_install
    if not mod_install.is_registered(root):
        return {"ok": True, "reason": "등록 기록 없음"}
    return mod_install.unregister(root)


def run(root, purge_harness=False, out=print):
    root = Path(root).resolve()
    cfg = read_json(paths.harness_dir(root) / "config.json", {}) or {}
    pid = cfg.get("project_id")
    results = {}
    for name, cands, args in (
            ("훅", [("vmc_harness.hooks.install", "unregister")], (str(root),)),
            ("브레인 MCP", [("vmc_harness.brain.install", "unregister_mcp")], (str(root),)),
            ("스케줄러", [("vmc_harness.loop.scheduler", "unregister")], (str(root),)),
            ("commit hook", [("vmc_harness.task.trailers", "uninstall_commit_hook")], (str(root),)),
            ("QA 서브에이전트", [("vmc_harness.qa.install", "unregister")], (str(root),)),
            ("mod 패널", [("vmc_harness.bootstrap.remove", "_mod_unregister")], (str(root),))):
        st, r = util.optional_call(cands, *args)
        results[name] = st
        out("%s: %s" % (name, {"ok": "해제", "skipped": "건너뜀(해당 영역 없음)"}.get(st, "오류 — %s" % r)))
    results["표시 블록"] = _strip_block(root)
    out("AGENTS.md 표시 블록: %s" % ("지움" if results["표시 블록"] else "없음"))
    if "CLAUDE.md" not in (cfg.get("created_files") or []) and _strip_claude_import(root, cfg):
        out("CLAUDE.md: init이 덧붙인 `@AGENTS.md` 한 줄을 지움")
    for rel in cfg.get("created_files") or []:
        p = root / rel
        if not p.is_file():
            continue
        t = p.read_text(encoding="utf-8").strip()
        if (rel == "CLAUDE.md" and t == "@AGENTS.md") or (rel == "AGENTS.md" and not t):
            p.unlink()
            out("%s: init이 만든 그대로라 지움" % rel)
        else:
            out("%s: 그 뒤 내용이 바뀌어 남김" % rel)
    if pid:
        ld = paths.local_project_dir(pid)
        if ld.exists():
            shutil.rmtree(str(ld), ignore_errors=True)
        out("로컬 원본 %s: 지움" % ld)
        results["local"] = str(ld)
    hd = paths.harness_dir(root)
    if purge_harness and hd.exists():
        shutil.rmtree(str(hd), ignore_errors=True)
        out(".harness/: 지움(--purge-harness)")
        results["harness_dir"] = "deleted"
    else:
        out(".harness/: 남김(팀 장부 — 지우려면 --purge-harness)")
        results["harness_dir"] = "kept"
    return results
