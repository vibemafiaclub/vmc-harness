"""작업·세션 상태 (로컬 원본 — PC 안 `~/.vmc-harness/projects/<project_id>/`).

- `tasks/<task_id>.json`        작업 상태(착수 선언·편집 경로·PM·과설계 점검 진행)
- `tasks/<task_id>.bg.json`     PM + 카드 검색 비동기 결과
- `tasks/<task_id>.od.json`     과설계 LLM 판정 비동기 결과
- `tasks/<task_id>.base.json`   착수 스냅샷의 "이미 바뀌어 있던 파일" 목록(base.dirty) — 작업 파일과 따로 둔다.
                                작업 트리 변경이 많으면 수 MB라, 훅마다 모든 작업 파일을 읽는 비용이 작업 수에 비례해 늘었다
- `sessions/<session_id>.json`  훅이 남긴 세션 상태(에이전트·transcript 경로·마지막 활동)

다른 영역 인터페이스(docs/interfaces.md): current(root, session_id), by_id(root, task_id).
"""

import copy
import hashlib
import os
from pathlib import Path

from vmc_harness.core.clock import now, now_iso, parse_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.project import local_dir

# 순서가 중요하다: Codex 값이 Claude 값보다 먼저 — Claude 안에서 띄운 Codex는 바깥 Claude의
# CLAUDE_CODE_SESSION_ID를 물려받는다(실측: 그 값으로 찾으면 "착수 선언 없음"으로 막힘). Codex 0.146은 CODEX_THREAD_ID.
SESSION_ENV = ("VMC_HARNESS_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
               "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID")


def _tasks_dir(root):
    d = local_dir(root) / "tasks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _sessions_dir(root):
    d = local_dir(root) / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe(name):
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(name))


def task_path(root, task_id, suffix=""):
    return _tasks_dir(root) / ("%s%s.json" % (_safe(task_id), suffix))


# ---- 세션 ----

def session_get(root, session_id):
    if not session_id:
        return None
    return read_json(_sessions_dir(root) / ("%s.json" % _safe(session_id)))


def session_touch(root, session_id, agent=None, transcript_path=None, cwd=None, prompt=False, start=False):
    """훅이 부른다. 세션 상태 갱신 후 돌려준다."""
    if not session_id:
        return None
    p = _sessions_dir(root) / ("%s.json" % _safe(session_id))
    rec = read_json(p) or {"session_id": session_id, "started_at": now_iso(), "prompts": 0}
    rec["last_seen"] = now_iso()
    if agent:
        rec["agent"] = agent
    if transcript_path:
        rec["transcript_path"] = transcript_path
    if cwd:
        rec["cwd"] = cwd
    if prompt:
        rec["prompts"] = int(rec.get("prompts") or 0) + 1
        rec["last_prompt_at"] = rec["last_seen"]
    if start:
        rec["start_events"] = int(rec.get("start_events") or 0) + 1
    write_json(p, rec)
    return rec


def sessions(root):
    d = _sessions_dir(root)
    out = []
    for f in d.glob("*.json"):
        r = read_json(f)
        if r:
            out.append(r)
    out.sort(key=lambda r: r.get("last_seen") or "", reverse=True)
    return out


def latest_session(root):
    s = sessions(root)
    return s[0] if s else None


def env_session_id():
    """에이전트 셸 안의 세션 id(SESSION_ENV 순서). 없으면 None. 세션 id 해석은 이 함수와 resolve_session_id 하나로."""
    for k in SESSION_ENV:
        v = os.environ.get(k)
        if v:
            return v
    return None


def resolve_session_id(root, explicit=None):
    """명시값 → 에이전트 환경변수(env_session_id) → 프로젝트의 가장 최근 훅 세션."""
    if explicit:
        return explicit
    v = env_session_id()
    if v:
        return v
    s = latest_session(root)
    return s["session_id"] if s else None


def prune_sessions(root, keep_days):
    cutoff = now().timestamp() - keep_days * 86400
    for f in _sessions_dir(root).glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


# ---- 작업 ----

_DIRTY_SUFFIX = ".base"
# 이 프로세스(훅 1회) 안에서 작업 파일을 다시 읽지 않는다. 키는 파일, 값은 (mtime_ns, size, 레코드).
# 다른 프로세스가 고친 파일은 수정 시각·크기가 바뀌어 다시 읽힌다. 돌려줄 때는 사본(호출자가 고쳐도 캐시가 안 바뀜).
_CACHE = {}


def _strip_dirty(root, task):
    """저장할 사본과 새로 쓸 사이드 파일 내용(쓸 필요 없으면 None).
    base.dirty는 사이드 파일로 빼고 작업 파일에는 표시(sidecar)만 남긴다. 내용이 바뀐 경우(_dirty_changed — take_base·
    absorb_outside_changes가 붙인다)나 사이드 파일이 아직 없을 때만 다시 쓴다(훅마다 수 MB를 쓰지 않게)."""
    base = task.get("base")
    if not isinstance(base, dict) or "dirty" not in base:
        if isinstance(base, dict):
            base.pop("_dirty_changed", None)
        return task, None
    changed = base.pop("_dirty_changed", False)
    rec = dict(task)
    b = dict(base)
    dirty = b.pop("dirty")
    b["sidecar"] = task["task_id"]
    rec["base"] = b
    if changed or not task_path(root, task["task_id"], _DIRTY_SUFFIX).is_file():
        return rec, dirty
    return rec, None


def save(root, task):
    task["updated_at"] = now_iso()
    rec, dirty = _strip_dirty(root, task)
    if dirty is not None:
        write_json(task_path(root, task["task_id"], _DIRTY_SUFFIX), dirty)
    p = write_json(task_path(root, task["task_id"]), rec)
    _remember(p, rec)
    return task


def _remember(p, rec):
    try:
        stt = Path(p).stat()
        _CACHE[str(p)] = (stt.st_mtime_ns, stt.st_size, copy.deepcopy(rec))
    except OSError:
        _CACHE.pop(str(p), None)


def _load(p, stt=None):
    """작업 파일 하나(캐시). 옛 형식(base.dirty가 안에 있음)은 읽을 때 사이드 파일로 옮긴다(1회)."""
    try:
        stt = stt or p.stat()
    except OSError:
        return None
    hit = _CACHE.get(str(p))
    if hit and hit[0] == stt.st_mtime_ns and hit[1] == stt.st_size:
        return hit[2]
    try:
        rec = read_json(p)
    except (ValueError, OSError):
        return None
    if not rec or not rec.get("task_id"):
        return None
    base = rec.get("base")
    if isinstance(base, dict) and isinstance(base.get("dirty"), dict):
        try:
            write_json(p.parent / ("%s%s.json" % (p.stem, _DIRTY_SUFFIX)), base["dirty"])
            slim = dict(rec)
            b = dict(base)
            b.pop("dirty")
            b["sidecar"] = rec["task_id"]
            slim["base"] = b
            p = write_json(p, slim)
            rec = slim
            stt = Path(p).stat()
        except OSError:
            pass
    _CACHE[str(p)] = (stt.st_mtime_ns, stt.st_size, rec)
    return rec


def _copy(rec):
    return copy.deepcopy(rec) if rec is not None else None


def _archive_dir(root):
    return _tasks_dir(root) / "archive"


def by_id(root, task_id):
    """작업 하나 — 보관(archive)으로 옮긴 옛 작업도 찾는다."""
    if not task_id:
        return None
    p = task_path(root, task_id)
    if not p.is_file():
        p = _archive_dir(root) / p.name
    return _copy(_load(p))


def base_dirty(root, task_id):
    """착수 스냅샷의 base.dirty(사이드 파일). 없으면 None."""
    if not task_id:
        return None
    p = task_path(root, task_id, _DIRTY_SUFFIX)
    if not p.is_file():
        p = _archive_dir(root) / p.name
    return read_json(p)


def archive_old(root, days=None):
    """끝난 지 task.archive_after_days가 지난 작업(측정까지 끝난 것)을 tasks/archive/로 옮긴다 — 훅마다 읽는 작업 파일 수를
    최근 것으로 묶는다(작업이 쌓일수록 모든 훅이 느려졌다). 실측 이력은 장부(ledger.read_tasks)가 원본이라 잃는 것이 없다.
    by_id·base_dirty·all_tasks(archived=True)는 보관 작업도 본다. 옮긴 작업 id 목록."""
    from vmc_harness.core import config
    days = config.get("task.archive_after_days", root) if days is None else days
    cutoff = now().timestamp() - float(days) * 86400
    moved = []
    for t in _all(root):
        end = parse_iso(t.get("ended_at")) if t.get("ended_at") else None
        if not end or t.get("measure_pending") or end.timestamp() >= cutoff:
            continue
        dest = _archive_dir(root)
        dest.mkdir(parents=True, exist_ok=True)
        for suffix in ("", _DIRTY_SUFFIX):
            src = task_path(root, t["task_id"], suffix)
            if src.is_file():
                try:
                    os.replace(str(src), str(dest / src.name))
                except OSError:
                    pass
        _CACHE.pop(str(task_path(root, t["task_id"])), None)
        moved.append(t["task_id"])
    return moved


def _all(root, archived=False):
    """캐시된 레코드 그대로(고치지 말 것) — 거른 뒤 필요한 것만 사본으로 돌려준다."""
    out = []
    files = list(_tasks_dir(root).glob("*.json"))
    if archived and _archive_dir(root).is_dir():
        files += list(_archive_dir(root).glob("*.json"))
    for f in files:
        if "." in f.stem:  # <id>.bg / .od / .report / .base 부속 파일
            continue
        r = _load(f)
        if r:
            out.append(r)
    out.sort(key=lambda r: r.get("started_at") or "")
    return out


def all_tasks(root, archived=False):
    """작업 전부(착수 순). archived=True면 보관으로 옮긴 옛 작업까지."""
    return [_copy(r) for r in _all(root, archived)]


def open_tasks(root, include_idle=False):
    """끝나지 않은 작업. 마지막 활동 뒤 task.finalize_idle_min이 지난 것은 끝난 것으로 본다(include_idle면 포함)."""
    from vmc_harness.task import lifecycle
    out = [_copy(t) for t in _all(root) if not t.get("ended_at")]
    if include_idle:
        return out
    return [t for t in out if not lifecycle.is_idle(root, t)]


def session_tasks(root, session_id):
    """이 세션의 작업(끝난 것 포함) — 착수 순."""
    return [_copy(t) for t in _all(root) if session_id and t.get("session_id") == session_id]


def current(root, session_id, reopen=False):
    """지금 세션의 (끝나지 않은) 작업. 세션 없이 선언된 작업이 있으면 이 세션이 가져간다.
    reopen=True(도구 활동): 이 세션의 마지막 작업이 조용해서·세션이 끝나서 마감됐으면 다시 열어 이어 붙인다."""
    tasks = open_tasks(root)
    if session_id:
        mine = [t for t in tasks if t.get("session_id") == session_id]
        if mine:
            return mine[-1]
        if reopen:
            from vmc_harness.task import lifecycle
            t = lifecycle.reopen_for_session(root, session_id)
            if t:
                return t
        unbound = [t for t in tasks if not t.get("session_id")]
        if unbound:
            t = unbound[-1]
            t["session_id"] = session_id
            save(root, t)
            return t
        return None
    unbound = [t for t in tasks if not t.get("session_id")]
    return unbound[-1] if unbound else None


def file_hash(path):
    h = hashlib.sha1()
    try:
        with open(str(path), "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def add_overhead(root, task_id, sec=0.0, tokens=0):
    """하네스가 더한 시간·토큰을 작업에 누적(09 harness_overhead)."""
    t = by_id(root, task_id)
    if not t:
        return None
    o = t.setdefault("overhead", {"sec": 0.0, "tokens": 0})
    o["sec"] = round(float(o.get("sec") or 0) + float(sec or 0), 3)
    o["tokens"] = int(o.get("tokens") or 0) + int(tokens or 0)
    return save(root, t)


def note_action(root, task_id, action_id):
    """이 작업 안에서 만든 하네스 조치(커밋 trailer `Harness-Action`)."""
    t = by_id(root, task_id)
    if not t:
        return None
    acts = t.setdefault("actions", [])
    if action_id not in acts:
        acts.append(action_id)
    return save(root, t)


def idle_minutes(rec_time):
    d = parse_iso(rec_time) if rec_time else None
    if not d:
        return None
    return (now() - d).total_seconds() / 60.0


def rel(root, p):
    """경로를 프로젝트 루트 상대 '/' 구분으로."""
    if not p:
        return p
    s = str(p).replace("\\", "/")
    try:
        pp = Path(p)
        if pp.is_absolute():
            r = Path(root).resolve()
            try:
                s = pp.resolve().relative_to(r).as_posix()
            except ValueError:
                return s
    except (OSError, RuntimeError):
        pass
    while s.startswith("./"):
        s = s[2:]
    return s
