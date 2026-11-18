"""mod 설치·해제 (spec 08 §화면, 10). 라이선스 CLI가 mod 폴더를 내려놓고 **로컬 경로로** 설치한다(GitHub 마켓플레이스 안 씀).

Claude Code 2.1.301 plugin-authoring 문서의 방식: mod 폴더 자체가 마켓플레이스(`.claude-plugin/marketplace.json`, source "./").
- `claude plugin marketplace add <mod 폴더>` → 그 폴더를 직접 읽는다(설치 사본이 아니라 폴더 자체 — 커널 업데이트가 바로 반영)
- `claude plugin install vmc-harness@vmc-harness-local --scope local` (프로젝트 cwd) → 그 프로젝트의 로컬 설정에만 켜진다
  (프로젝트 공유 설정 `.claude/settings.json`을 고치지 않는다 — 경로가 PC마다 다르다)
- 해제: `claude plugin uninstall … --scope local`
mod 폴더 찾기: VMC_HARNESS_MOD_DIR → `~/.vmc-harness/kernel/current/mod` → 소스 레포의 `mod/`.
"""

import json
import shutil
from pathlib import Path

from vmc_harness.core import paths
from vmc_harness.core.clock import now_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.proc import run

PLUGIN = "vmc-harness"
TIMEOUT_S = 120


def mod_dir():
    import os
    env = os.environ.get("VMC_HARNESS_MOD_DIR")
    cands = [Path(env)] if env else []
    cands += [paths.kernel_root() / "current" / "mod", Path(__file__).resolve().parents[3] / "mod"]
    for c in cands:
        if (c / ".claude-plugin" / "plugin.json").is_file():
            return c
    return None


def marketplace_name(d=None):
    d = d or mod_dir()
    data = read_json(Path(d) / ".claude-plugin" / "marketplace.json", {}) if d else {}
    return (data or {}).get("name") or "vmc-harness-local"


def _state_path(root):
    from vmc_harness.core.project import local_dir
    return local_dir(root) / "mod.json"


def is_registered(root):
    """등록 기록이 있고 그 mod 폴더가 아직 있을 때만 참. 커널 업데이트로 옛 버전 폴더가 지워지면 패널은 뜨지 않는데
    기록만 남아 완료 보고 아래 2줄까지 꺼지던 문제(2026-11-18 실측: 지워진 0.1.7 경로)."""
    try:
        st = read_json(_state_path(root), {}) or {}
    except RuntimeError:
        return False
    if not st.get("registered"):
        return False
    d = st.get("mod_dir")
    return not d or (Path(d) / ".claude-plugin" / "plugin.json").is_file()


def _claude():
    return shutil.which("claude")


def register(root):
    """→ {"ok":bool,"reason":str|None,"mod_dir":str}. claude가 없으면 ok=False(mod 없이도 brief·HTML로 본다)."""
    exe = _claude()
    d = mod_dir()
    if not exe:
        return {"ok": False, "reason": "claude 없음", "mod_dir": str(d) if d else None}
    if not d:
        return {"ok": False, "reason": "mod 폴더 없음", "mod_dir": None}
    mk = marketplace_name(d)
    rc, out, err = run([exe, "plugin", "marketplace", "add", str(d), "--json"], cwd=str(root), timeout=TIMEOUT_S)
    if rc != 0 and "already" not in (out + err).lower():
        # 같은 이름이 다른 경로로 등록돼 있으면(커널 버전 경로) 지우고 다시
        run([exe, "plugin", "marketplace", "remove", mk], cwd=str(root), timeout=TIMEOUT_S)
        rc, out, err = run([exe, "plugin", "marketplace", "add", str(d), "--json"], cwd=str(root), timeout=TIMEOUT_S)
        if rc != 0:
            return {"ok": False, "reason": "marketplace add 실패: %s" % (err or out).strip()[:300], "mod_dir": str(d)}
    rc, out, err = run([exe, "plugin", "install", "%s@%s" % (PLUGIN, mk), "--scope", "local", "--json"],
                       cwd=str(root), timeout=TIMEOUT_S)
    if rc != 0 and "already" not in (out + err).lower():
        return {"ok": False, "reason": "install 실패: %s" % (err or out).strip()[:300], "mod_dir": str(d)}
    write_json(_state_path(root), {"registered": True, "at": now_iso(), "mod_dir": str(d), "marketplace": mk})
    return {"ok": True, "reason": None, "mod_dir": str(d)}


def unregister(root):
    exe = _claude()
    st = {}
    try:
        st = read_json(_state_path(root), {}) or {}
    except RuntimeError:
        pass
    mk = st.get("marketplace") or marketplace_name()
    result = {"ok": True, "reason": None}
    if exe:
        rc, out, err = run([exe, "plugin", "uninstall", "%s@%s" % (PLUGIN, mk), "--scope", "local", "--json"],
                           cwd=str(root), timeout=TIMEOUT_S)
        if rc != 0 and "not" not in (out + err).lower():
            result = {"ok": False, "reason": (err or out).strip()[:300]}
    try:
        p = _state_path(root)
        if p.is_file():
            p.unlink()
    except (OSError, RuntimeError):
        pass
    return result
