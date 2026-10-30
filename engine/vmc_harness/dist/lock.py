"""프로젝트당 클렌징 루프 1개 잠금(spec 06·10).

- 서버(POST/DELETE /harness/lock)가 PC 사이를 막는다. 로컬 잠금 파일은 같은 PC의 두 번째 루프를 막는다(서버는 같은 PC면 연장해 주므로)
- 서버에 못 닿거나 라이선스가 없으면 로컬 파일만으로 진행한다(편의 장치 fail-open, 01 P8)
- 서버가 "다른 PC가 쥐고 있음"이라고 답하면 False
"""

import hashlib
import os

from vmc_harness.core import config
from vmc_harness.core.clock import now, parse_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.project import local_dir, pc_id, project_id


def project_hash(root):
    return hashlib.sha256((project_id(root) or "").encode("utf-8")).hexdigest()


def _file(root):
    return local_dir(root) / "loop.lock"


def _live_local(root):
    rec = read_json(_file(root))
    if not rec:
        return None
    exp = parse_iso(rec.get("expires_at"))
    if exp and exp > now():
        return rec
    return None


def _write_local(root, expires_at, mode):
    """원자적으로 새로 만든다(이미 살아 있는 잠금이 있으면 False)."""
    p = _file(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"pc_id": pc_id(), "pid": os.getpid(), "expires_at": expires_at, "mode": mode, "at": now().isoformat(timespec="seconds")}
    if p.exists() and not _live_local(root):
        try:
            p.unlink()
        except OSError:
            return False
    try:
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    os.close(fd)
    write_json(p, rec)
    return True


def acquire(root, ttl_s):
    """잠금을 얻으면 True."""
    from vmc_harness.dist import server
    if _live_local(root):
        return False
    import datetime as _dt
    ttl = min(int(ttl_s), config.get("lock.local_stale_s", root))
    st, data = server.call("POST", "/harness/lock", {"project_hash": project_hash(root), "pc_id": pc_id(), "ttl_s": ttl})
    if st == 200 and isinstance(data, dict):
        if not data.get("granted"):
            return False
        mode, expires_at = "server", data.get("expires_at")
    else:
        mode, expires_at = "local", (now() + _dt.timedelta(seconds=ttl)).isoformat(timespec="seconds")
    if not _write_local(root, expires_at, mode):
        if mode == "server":
            server.call("DELETE", "/harness/lock", {"project_hash": project_hash(root), "pc_id": pc_id()})
        return False
    return True


def release(root):
    from vmc_harness.dist import server
    rec = read_json(_file(root))
    if rec and rec.get("pc_id") == pc_id() and rec.get("mode") == "server":
        server.call("DELETE", "/harness/lock", {"project_hash": project_hash(root), "pc_id": pc_id()})
    if rec and rec.get("pc_id") == pc_id():
        try:
            _file(root).unlink()
        except OSError:
            pass
