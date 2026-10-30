"""커널 설치 위치 `~/.vmc-harness/kernel/<버전>/`와 `kernel/current`(지금 버전 이름이 든 텍스트 파일) 해석.

실제 로직은 껍데기(shell.py)에 한 벌 — 껍데기는 커널 없이도 돌아야 해서 거기 둔다. 여기는 엔진 쪽 이름.
"""

from vmc_harness.dist.shell import (  # noqa: F401 — 다른 영역이 이 이름으로 부른다
    current_dir,
    current_version,
    engine_dir,
    installed_versions,
    kernel_root,
    launcher_path,
    bin_dir,
    set_current,
)


def running_from_kernel():
    """지금 도는 엔진이 설치된 커널(읽기 전용)인가 — 개발 체크아웃이면 False."""
    from pathlib import Path
    import vmc_harness
    here = Path(vmc_harness.__file__).resolve()
    try:
        here.relative_to(kernel_root().resolve())
        return True
    except ValueError:
        return False
