"""저장 위치 (spec 09 §저장 구역).

- 사용자 홈 `~/.vmc-harness/` (환경변수 VMC_HARNESS_HOME으로 바꿀 수 있다 — 테스트용)
- 프로젝트 레포 `.harness/`
- 로컬 원본 `~/.vmc-harness/projects/<project_id>/`
"""

import os
from pathlib import Path


def home():
    p = os.environ.get("VMC_HARNESS_HOME")
    return Path(p).expanduser() if p else Path.home() / ".vmc-harness"


def kernel_root():
    return home() / "kernel"


def brain_dir():
    return home() / "brain"


def pc_file():
    return home() / "pc.json"


def license_file():
    return home() / "license.json"


def find_project_root(start=None):
    """start에서 위로 올라가며 `.harness/config.json`이 있는 폴더를 찾는다. 없으면 None."""
    cur = Path(start or os.getcwd()).resolve()
    for d in [cur] + list(cur.parents):
        if (d / ".harness" / "config.json").is_file():
            return d
    return None


def harness_dir(root):
    return Path(root) / ".harness"


def ledger_dir(root):
    return harness_dir(root) / "ledger"


def cache_dir(root):
    return harness_dir(root) / "cache"


def local_project_dir(project_id):
    return home() / "projects" / project_id


def ensure(p):
    Path(p).mkdir(parents=True, exist_ok=True)
    return Path(p)
