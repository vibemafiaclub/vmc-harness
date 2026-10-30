"""프로젝트·PC 식별 (spec 09)."""

import platform

from vmc_harness.core import paths
from vmc_harness.core.clock import now_iso
from vmc_harness.core.ids import new_uuid
from vmc_harness.core.jsonio import read_json, write_json


def pc_id():
    data = read_json(paths.pc_file(), None)
    if not data or not data.get("pc_id"):
        data = {"pc_id": new_uuid(), "os": platform.system().lower(), "created_at": now_iso()}
        write_json(paths.pc_file(), data)
    return data["pc_id"]


def load_config(root):
    return read_json(paths.harness_dir(root) / "config.json", {}) or {}


def project_id(root):
    return load_config(root).get("project_id")


def local_dir(root):
    """이 프로젝트의 로컬 원본 폴더 `~/.vmc-harness/projects/<project_id>/`."""
    pid = project_id(root)
    if not pid:
        raise RuntimeError("하네스가 설치되지 않은 프로젝트")
    return paths.ensure(paths.local_project_dir(pid))


def author_name(root=None):
    """이 PC 사람 이름 — 작성자 기록·"본인 화면" 비교의 기준은 이 함수 하나(render·brain 공통).
    프로젝트가 있으면 그 레포의 `git config user.name`(로컬 설정이 전역보다 우선), 없으면 전역, 그것도 없으면 OS 사용자."""
    import os
    from vmc_harness.core.proc import run
    args = ["git", "config", "user.name"] if root else ["git", "config", "--global", "user.name"]
    rc, out, _ = run(args, cwd=str(root) if root else None, timeout=5)
    if rc == 0 and out.strip():
        return out.strip()
    if root:
        rc, out, _ = run(["git", "config", "--global", "user.name"], timeout=5)
        if rc == 0 and out.strip():
            return out.strip()
    return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
