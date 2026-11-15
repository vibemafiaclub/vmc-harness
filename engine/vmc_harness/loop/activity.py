"""마지막 활동 시각·퇴근 신호·복귀 양보 (spec 06·09 `activity.json`).

- touch: UserPromptSubmit 훅(사람 지시)마다. 루프가 돌고 있으면 중단 신호 파일을 만든다(복귀 즉시 양보)
- request_start: `start-loop`(퇴근 입력)
- 루프는 작은 단위마다 stop_requested()를 보고, 참이면 하던 단위를 버리고 대기열로 돌린다
"""

import datetime as _dt

from vmc_harness.core import config
from vmc_harness.core.clock import now, now_iso, parse_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.project import local_dir


def activity_path(root):
    return local_dir(root) / "activity.json"


def stop_path(root):
    return local_dir(root) / "loop-stop"


def running_path(root):
    return local_dir(root) / "loop-running.json"


def load(root):
    return read_json(activity_path(root), None) or {}


def _save(root, data):
    write_json(activity_path(root), data)


def touch(root, at=None):
    """사람 지시가 들어왔다. 루프가 돌고 있으면 양보 신호. 퇴근 요청은 지운다(돌아왔다)."""
    data = load(root)
    data["last_prompt_at"] = at or now_iso()
    data.pop("start_requested_at", None)
    _save(root, data)
    if is_running(root) and not (read_json(running_path(root), None) or {}).get("no_yield"):
        signal_stop(root, "human_prompt")  # 진단 자동 반영 처리(no_yield — worktree만 쓴다)는 사람 지시에 양보하지 않는다
    return data


def session_started(root):
    data = load(root)
    data["last_session_start_at"] = now_iso()
    _save(root, data)
    return data


def request_start(root):
    data = load(root)
    data["start_requested_at"] = now_iso()
    _save(root, data)
    clear_stop(root)
    return data


def start_requested(root):
    return bool(load(root).get("start_requested_at"))


def signal_stop(root, reason):
    write_json(stop_path(root), {"at": now_iso(), "reason": reason})


def stop_requested(root):
    return stop_path(root).exists()


def clear_stop(root):
    try:
        stop_path(root).unlink()
    except OSError:
        pass


def mark_running(root, info):
    write_json(running_path(root), dict(info, since=now_iso()))


def clear_running(root):
    try:
        running_path(root).unlink()
    except OSError:
        pass


def is_running(root):
    info = read_json(running_path(root), None)
    if not info:
        return False
    pid = info.get("pid")
    if pid and not _pid_alive(pid):
        return False
    since = parse_iso(info.get("since"))
    ttl = config.get("loop.lock_ttl_s", root)
    if since and (now() - since).total_seconds() > ttl and not info.get("heartbeat_at"):
        return False
    hb = parse_iso(info.get("heartbeat_at"))
    if hb and (now() - hb).total_seconds() > ttl:
        return False
    return True


def heartbeat(root):
    info = read_json(running_path(root), None)
    if info:
        info["heartbeat_at"] = now_iso()
        write_json(running_path(root), info)


def _pid_alive(pid):
    import os
    import sys
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if sys.platform == "win32":
        from vmc_harness.core.proc import run
        rc, out, _ = run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"])
        return rc == 0 and str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


# ---- 시간 창 ----

def in_night_window(root, t=None):
    """start_hour 이후 ~ 다음 날 stop_hour 전."""
    t = t or now()
    start, stop = config.get("loop.start_hour", root), config.get("loop.stop_hour", root)
    h = t.hour
    if start > stop:
        return h >= start or h < stop
    return start <= h < stop


def next_stop_at(root, t=None):
    """지금부터 처음 오는 stop_hour 정각 — 무조건 중지 시각."""
    t = t or now()
    stop = config.get("loop.stop_hour", root)
    cand = t.replace(hour=stop, minute=0, second=0, microsecond=0)
    if cand <= t:
        cand = cand + _dt.timedelta(days=1)
    return cand


def is_idle(root, t=None):
    """퇴근 판정: 설정 시각 이후이고 마지막 지시가 idle_hours 이상 전."""
    t = t or now()
    if not in_night_window(root, t):
        return False
    last = parse_iso(load(root).get("last_prompt_at"))
    if last is None:
        return True
    return (t - last).total_seconds() >= config.get("loop.idle_hours", root) * 3600


def last_human_at(root):
    """사람이 마지막으로 이 프로젝트에서 AI에게 일을 시킨 시각(datetime | None).
    activity.json(UserPromptSubmit·세션 시작 훅)과 부트스트랩·재진단이 쓴 세션 색인(사람 프롬프트·main 세션 시작) 중 늦은 것.
    health.json activity_last는 하네스 자신의 이벤트도 섞여 사람 신호로 쓰지 않는다."""
    def _t(v):
        return parse_iso(v) if isinstance(v, str) else None
    data = load(root)
    times = [_t(data.get(k)) for k in ("last_prompt_at", "last_session_start_at")]
    from vmc_harness.core.jsonio import read_jsonl
    for r in read_jsonl(local_dir(root) / "sessions-index.jsonl"):
        if not isinstance(r, dict) or r.get("kind") != "main":
            continue
        times.append(_t(r.get("started_at")))
        times.extend(_t(x.get("at")) for x in r.get("human_prompts") or [] if isinstance(x, dict))
    times = [t if t.tzinfo else t.astimezone() for t in times if t]
    return max(times) if times else None


def recently_used(root, t=None):
    """사람 활동이 loop.idle_skip_days 안에 있었나. 한 번도 없으면 False — 아무도 안 쓰는 프로젝트에 밤 토큰을 쓰지 않는다."""
    t = t or now()
    last = last_human_at(root)
    return last is not None and (t - last).total_seconds() < config.get("loop.idle_skip_days", root) * 86400


def night_key(root, t=None):
    """밤 묶음 키(YYYY-MM-DD, 밤이 시작된 날). stop_hour 전 새벽은 전날 밤."""
    t = t or now()
    if t.hour < config.get("loop.stop_hour", root):
        t = t - _dt.timedelta(days=1)
    return t.strftime("%Y-%m-%d")
