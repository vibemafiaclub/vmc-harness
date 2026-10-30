"""루프 안에서 오래 걸리는 실행(`claude -p`, 테스트)을 양보 가능하게 돌린다.

중단 신호 파일(사람 복귀)·마감(stop_hour)·시간 상한 중 하나라도 걸리면 자식 프로세스를 끊고 interrupted로 돌려준다.
파이프 교착을 피하려고 입출력은 임시 파일로.
"""

import os
import subprocess
import sys
import tempfile
import time

from vmc_harness.core import config
from vmc_harness.core.clock import now


def run(args, cwd=None, timeout=None, input_text=None, env=None, root=None, deadline=None):
    """(rc, stdout, stderr, interrupted: None|'stop'|'deadline'|'timeout')."""
    from vmc_harness.loop import activity
    poll = config.get("loop.poll_s", root) if root else 1
    tmpdir = tempfile.mkdtemp(prefix="vmh-loop-")
    fin = os.path.join(tmpdir, "in")
    fout = os.path.join(tmpdir, "out")
    ferr = os.path.join(tmpdir, "err")
    with open(fin, "w", encoding="utf-8") as f:
        f.write(input_text or "")
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    t0 = time.time()
    interrupted = None
    try:
        with open(fin, "rb") as i, open(fout, "wb") as o, open(ferr, "wb") as e:
            try:
                p = subprocess.Popen(args, cwd=cwd, env=env, stdin=i, stdout=o, stderr=e, **kw)
            except FileNotFoundError as ex:
                return -127, "", str(ex), None
            while True:
                try:
                    p.wait(timeout=poll)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if root and activity.stop_requested(root):
                    interrupted = "stop"
                elif deadline and now() >= deadline:
                    interrupted = "deadline"
                elif timeout and time.time() - t0 > timeout:
                    interrupted = "timeout"
                if interrupted:
                    _kill(p)
                    break
        rc = p.returncode if p.returncode is not None else -9
        with open(fout, "r", encoding="utf-8", errors="replace") as f:
            out = f.read()
        with open(ferr, "r", encoding="utf-8", errors="replace") as f:
            err = f.read()
        return rc, out, err, interrupted
    finally:
        for fp in (fin, fout, ferr):
            try:
                os.unlink(fp)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass


def _kill(p):
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        else:
            import signal
            os.killpg(p.pid, signal.SIGTERM)
        p.wait(timeout=10)
    except Exception:
        try:
            p.kill()
            p.wait(timeout=5)
        except Exception:
            pass
