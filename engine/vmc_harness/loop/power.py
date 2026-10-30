"""OS 절전과 야간 루프 (spec 06 실행).

- 화면 잠금은 루프를 멈추지 않는다. 멈추는 건 절전·종료다.
- 루프 run 동안 절전을 막는다: Windows `SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED)`,
  macOS `caffeinate -i -w <pid>` 자식 프로세스. 실패해도 루프는 진행한다(결과만 기록).
- init 때 절전 설정을 읽어 루프 시작 조건(`loop.idle_hours` 무지시)보다 절전이 먼저 오면 알림(must) 1건.
  Windows `powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE`의 AC 값(초), macOS `pmset -g`의 `sleep`(분, 0=안 함).
  판단 못 하면 조용히 넘어간다.
"""

import os
import re
import subprocess
import sys

from vmc_harness.core import config
from vmc_harness.core.proc import run

# Windows SetThreadExecutionState 플래그(OS 상수)
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


# ---- 루프 run 동안 절전 막기 ----

def _set_execution_state(flags):
    import ctypes
    return ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint(flags))


def inhibit():
    """절전 막기 시작. {"ok", "method", "reason", "_handle"} — 실패해도 예외를 내지 않는다."""
    try:
        if sys.platform == "win32":
            if not _set_execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
                return {"ok": False, "method": "SetThreadExecutionState", "reason": "반환값 0"}
            return {"ok": True, "method": "SetThreadExecutionState", "reason": None}
        if sys.platform == "darwin":
            p = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())], stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"ok": True, "method": "caffeinate", "reason": None, "_handle": p}
        return {"ok": False, "method": None, "reason": "지원하지 않는 OS: %s" % sys.platform}
    except Exception as e:  # 절전 막기 실패가 루프를 멈추지 않게 — 기록만
        return {"ok": False, "method": None, "reason": str(e)[:200]}


def release(hold):
    """inhibit()가 잡은 것을 푼다. 실패는 삼킨다."""
    if not hold or not hold.get("ok"):
        return
    try:
        if hold.get("method") == "SetThreadExecutionState":
            _set_execution_state(ES_CONTINUOUS)
        elif hold.get("_handle") is not None:
            p = hold["_handle"]
            if p.poll() is None:
                p.terminate()
                p.wait(timeout=config.get("loop.power_query_timeout_s"))
    except Exception:
        pass


def summary(hold):
    """기록용(핸들 제외)."""
    return {k: v for k, v in (hold or {}).items() if not k.startswith("_")}


# ---- 절전 설정 읽기 ----

def parse_powercfg(text):
    """powercfg /query 출력에서 AC 절전 시간(초). 못 찾으면 None. 한국어 출력도 'AC'·0x 값은 그대로다."""
    for line in (text or "").splitlines():
        if re.search(r"\bAC\b", line):
            m = re.search(r"0x([0-9a-fA-F]+)", line)
            if m:
                return int(m.group(1), 16)
    return None


def parse_pmset(text):
    """pmset -g 출력에서 sleep 값(분, 0=안 함). 못 찾으면 None."""
    m = re.search(r"^\s*sleep\s+(\d+)", text or "", re.MULTILINE)
    return int(m.group(1)) if m else None


def sleep_seconds():
    """이 PC의 유휴 절전까지 초(0=안 함). 판단 못 하면 None."""
    try:
        if sys.platform == "win32":
            rc, out, _ = run(["powercfg", "/query", "SCHEME_CURRENT", "SUB_SLEEP", "STANDBYIDLE"],
                             timeout=config.get("loop.power_query_timeout_s"))
            return parse_powercfg(out) if rc == 0 else None
        if sys.platform == "darwin":
            rc, out, _ = run(["pmset", "-g"], timeout=config.get("loop.power_query_timeout_s"))
            m = parse_pmset(out) if rc == 0 else None
            return None if m is None else m * 60
    except Exception:
        return None
    return None


def check_sleep(root):
    """절전이 루프 시작 조건보다 먼저 오면 알림(must). {"sleep_s", "warned"}"""
    s = sleep_seconds()
    idle_s = config.get("loop.idle_hours", root) * 3600
    warned = bool(s) and s < idle_s
    if warned:
        try:
            from vmc_harness.core import notices
            notices.add(root, "loop_sleep_first", "must",
                        "이 PC는 %d분 뒤 절전해요 — 야간 루프가 돌려면 절전을 끄거나 퇴근할 때 `vmc-harness start-loop`로 직접 시작하세요."
                        % (s // 60), refs={"sleep_s": s, "idle_s": idle_s})
        except Exception:
            pass
    return {"sleep_s": s, "warned": warned}
