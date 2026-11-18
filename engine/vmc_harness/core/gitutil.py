from vmc_harness.core.proc import run

# 한글 등 비ASCII 경로를 "\354\227…"로 이스케이프하지 않게(D3). 이름 목록은 -z로 받아 따옴표·줄바꿈 경로도 그대로
GIT_BASE = ["git", "-c", "core.quotepath=off"]


def git(root, *args, timeout=30):
    return run(GIT_BASE + list(args), cwd=str(root), timeout=timeout)


def head(root):
    rc, out, _ = git(root, "rev-parse", "HEAD")
    return out.strip() if rc == 0 else None


def split_z(out):
    """-z 출력 → 이름 목록."""
    return [x for x in (out or "").split("\0") if x]


def changed_files(root, base=None, paths=None):
    """작업 트리 변경(추적 + 미추적) 파일 목록. base가 있으면 base..작업트리. paths가 있으면 그 경로만 본다."""
    files = set()
    tail = ["--"] + [str(p) for p in paths] if paths else []
    rc, out, _ = git(root, "diff", "--name-only", "--no-renames", "-z", base or "HEAD", *tail)
    if rc == 0:
        files.update(split_z(out))
    rc, out, _ = git(root, "ls-files", "--others", "--exclude-standard", "-z", *tail)
    if rc == 0:
        files.update(split_z(out))
    return sorted(files)


def git_bytes(root, *args, timeout=30):
    """(returncode, stdout 바이트) — 파일 내용을 줄끝·인코딩 변환 없이 받을 때."""
    import subprocess
    from vmc_harness.core.proc import no_window
    try:
        r = subprocess.run(GIT_BASE + list(args), cwd=str(root), timeout=timeout, capture_output=True, **no_window())
        return r.returncode, r.stdout
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return -1, b""
