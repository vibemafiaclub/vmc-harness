"""사용자 홈 정리(spec 10 remove). bootstrap의 `remove`가 부른다.

remove_home(keep_brain=True): ~/.vmc-harness/ 아래를 지운다(커널은 읽기 전용이라 권한을 풀고). 브레인(L3, git)은 기본 보존.
PATH 등록(셸 rc 줄 / 윈도우 사용자 PATH)과 키체인의 라이선스 키도 지운다.
윈도우에서 실행 중인 vmc-harness.cmd 자신은 지워진 뒤 "배치 파일을 찾을 수 없음"이 한 줄 보일 수 있다(동작에는 영향 없음).
"""

from vmc_harness.dist import shell


def remove_home(keep_brain=True):
    """지운 항목 목록과 남긴 항목을 돌려준다."""
    h = shell.home()
    removed, kept = [], []
    shell.forget_key()
    try:
        removed += ["PATH:" + p for p in shell.unregister_path()]
    except Exception:
        pass
    if h.is_dir():
        for child in sorted(h.iterdir()):
            if keep_brain and child.name == "brain":
                kept.append(str(child))
                continue
            if child.is_dir():
                shell.rmtree(child)
            else:
                try:
                    child.unlink()
                except OSError:
                    continue
            removed.append(str(child))
        if not keep_brain or not (h / "brain").exists():
            try:
                h.rmdir()
            except OSError:
                pass
    return {"removed": removed, "kept": kept}
