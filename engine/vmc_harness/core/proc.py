"""외부 프로세스 실행. 셸 문법에 기대지 않는다(Windows·macOS 공통) — 인자는 리스트로.

Windows 창 플래그(Microsoft Learn "Process Creation Flags", 2025-07 판 확인):
- CREATE_NO_WINDOW(0x08000000): 콘솔 앱을 창 없는 콘솔로 띄운다. DETACHED_PROCESS·CREATE_NEW_CONSOLE과 같이 주면 무시된다.
- DETACHED_PROCESS(0x8): 콘솔 자체가 없다 → 그 자식이 부르는 콘솔 앱(git 등)마다 새 콘솔 창이 뜬다(0.1.4의 창 깜빡임).
  그래서 분리 실행은 DETACHED_PROCESS 대신 CREATE_NO_WINDOW — 자식은 자기만의 숨은 콘솔을 갖고, 손자는 그 콘솔을 물려받아 창이 안 뜬다.
  부모 콘솔을 공유하지 않으니 부모 창이 닫혀도(CTRL_CLOSE) 같이 죽지 않는다.
- CREATE_NEW_PROCESS_GROUP(0x200): 새 프로세스 그룹 — 부모 쪽 Ctrl+C가 자식에 가지 않는다.
- CREATE_BREAKAWAY_FROM_JOB(0x01000000): 부모가 job에 묶여 있으면 job 밖으로. job이 BREAKAWAY_OK를 안 주면 생성이 실패하므로
  그때는 이 플래그 없이 다시 띄운다. (Claude Code가 쓰는 libuv는 job에 SILENT_BREAKAWAY_OK를 줘서 훅의 자식이 이미 job 밖이다 —
  libuv src/win/process.c 확인. Codex(Rust)는 미확인 — 그래서 시도한다.)
고객 PC에서 실제 동작(창 안 뜸·훅 종료 뒤 자식 생존)은 미검증 — Windows CI에서 생존만 확인한다.
"""

import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def _win_flags(detached=False):
    """Windows creationflags. 비-Windows에서는 0."""
    if sys.platform != "win32":
        return 0
    return CREATE_NO_WINDOW | (CREATE_NEW_PROCESS_GROUP | CREATE_BREAKAWAY_FROM_JOB if detached else 0)


def no_window():
    """subprocess 직접 호출(바이트 출력 등)에 넘길 키워드 — Windows면 창 없는 콘솔, 아니면 빈 dict."""
    return {"creationflags": _win_flags()} if sys.platform == "win32" else {}


def run(args, cwd=None, timeout=None, input_text=None, env=None):
    """(returncode, stdout, stderr). 시간 초과면 returncode=-9, 실행 파일 없음이면 -127.
    Windows: 창 없는 콘솔로(부모가 콘솔 없는 백그라운드여도 git 등이 창을 띄우지 않게)."""
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = _win_flags()
    try:
        r = subprocess.run(
            args, cwd=cwd, timeout=timeout, input=input_text, env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", **kw
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return -9, out, "timeout"
    except FileNotFoundError as e:
        return -127, "", str(e)


_DETACHED = []


def _forget_detached():
    """종료 때: 일부러 분리한 자식은 기다리지 않는다(Popen 소멸자의 'still running' 경고를 내지 않게 끝난 것으로 표시)."""
    for p in _DETACHED:
        if p.poll() is None:
            p.returncode = 0
    del _DETACHED[:]


def spawn_detached(args, cwd=None, env=None):
    """부모와 분리된 백그라운드 실행(훅이 기다리지 않게). Popen 객체를 돌려준다.
    객체는 모듈 목록에 붙잡아 두고(끝난 것은 다음 호출 때 거둔다), 프로세스 종료 때 기다리지 않는다."""
    import atexit
    for p in list(_DETACHED):
        if p.poll() is not None:
            _DETACHED.remove(p)
    kw = {"cwd": cwd, "env": env, "stdin": subprocess.DEVNULL,
          "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kw["creationflags"] = _win_flags(detached=True)
    else:
        kw["start_new_session"] = True
    try:
        p = subprocess.Popen(args, **kw)
    except OSError:
        if sys.platform != "win32":
            raise
        kw["creationflags"] = _win_flags(detached=True) & ~CREATE_BREAKAWAY_FROM_JOB  # job이 이탈을 막으면(접근 거부)
        p = subprocess.Popen(args, **kw)
    if not _DETACHED and not getattr(spawn_detached, "_hooked", False):
        atexit.register(_forget_detached)
        spawn_detached._hooked = True
    _DETACHED.append(p)
    return p
