"""게이트 런타임 상태(캐시 — 다시 만들 수 있는 것). `.harness/cache/gates/`.

- 세션별: 최근 차단(그냥 진행 판정용), 1회 통과 허가, 완료 게이트 차단 횟수
- 작업 키: 착수 선언이 있으면 task_id, 없으면 `S-<session_id>`(증거 폴더 이름도 같은 규칙)
"""

import hashlib
import json
import os

from vmc_harness.core import paths
from vmc_harness.core.clock import now, now_iso, parse_iso
from vmc_harness.core.jsonio import read_json, write_json


def _file(root, session_id):
    sid = (session_id or "nosession").replace("/", "_").replace("\\", "_")
    return paths.cache_dir(root) / "gates" / ("session-%s.json" % sid)


def load(root, session_id):
    return read_json(_file(root, session_id), None) or {"blocks": [], "bypass": [], "completion_blocks": {}, "noticed": []}


def save(root, session_id, st):
    write_json(_file(root, session_id), st)


def fingerprint(ctx):
    """같은 도구 호출인지 — 도구 이름 + 쓰는 경로 + 셸 명령."""
    body = json.dumps({"tool": ctx.tool_name, "paths": sorted(ctx.write_paths or []),
                       "command": ctx.command or "", "event": ctx.event}, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def record_block(root, ctx, gate, reason, gate_id=None):
    ctx.state["recorded_block_gate"] = gate   # 디스패처가 사람에게 전할 풀기 한 줄을 고른다(gates.proceed.unblock_hint)
    st = load(root, ctx.session_id)
    st["blocks"].append({"gate": gate, "gate_id": gate_id, "at": now_iso(), "fingerprint": fingerprint(ctx),
                         "event": ctx.event, "reason": reason, "proceeded": False,
                         "paths": [p for p in (ctx.state.get("block_paths") or []) if isinstance(p, str)],
                         "task": task_key(root, ctx.session_id, ctx.task_id if isinstance(ctx.task_id, str) else None)})
    st["blocks"] = st["blocks"][-_keep():]
    save(root, ctx.session_id, st)


def _keep():
    from vmc_harness.core import config
    return int(config.get("gates.state_keep_blocks"))


def last_block(root, session_id):
    st = load(root, session_id)
    return st["blocks"][-1] if st["blocks"] else None


def grant_bypass(root, session_id, block):
    st = load(root, session_id)
    st["bypass"].append({"gate": block["gate"], "gate_id": block.get("gate_id"),
                         "fingerprint": block["fingerprint"], "event": block.get("event"), "at": now_iso(),
                         "paths": list(block.get("paths") or []), "task": block.get("task")})
    for b in st["blocks"]:
        if b["at"] == block["at"] and b["fingerprint"] == block["fingerprint"]:
            b["proceeded"] = True
    save(root, session_id, st)


def mark_disputed(root, session_id, block, reason):
    """에이전트 반박 표시(gates.proceed.dispute) — 통과 허가는 주지 않는다."""
    st = load(root, session_id)
    for b in st["blocks"]:
        if b["at"] == block["at"] and b["fingerprint"] == block["fingerprint"]:
            b["disputed"] = reason
    save(root, session_id, st)


def consume_bypass(root, ctx, gate, gate_id=None):
    """1회 통과 허가가 있으면 소비하고 True. 완료 게이트는 지문 대신 이벤트(Stop)로 맞춘다."""
    st = load(root, ctx.session_id)
    fp = fingerprint(ctx)
    for i, b in enumerate(st["bypass"]):
        if b["gate"] != gate or (gate_id and b.get("gate_id") not in (None, gate_id)):
            continue
        if b["fingerprint"] == fp or (gate == "completion" and ctx.event == "Stop"):
            st["bypass"].pop(i)
            save(root, ctx.session_id, st)
            return True
    return False


def file_bypassed(root, ctx, gate, rels, task=None):
    """"그냥 진행"이 그 파일들을 이 작업이 끝날 때까지 허용했는가 — 호출 1회가 아니라 (게이트, 파일, 작업) 단위.
    1회 통과는 같은 작업의 같은 파일 두 번째 수정(래칫 값 다음 주석 한 줄)에서 다시 막혔고, 게이트 단위로 소비돼
    다른 파일의 차단이 허가를 가져갔다(현장 결함 C9). 소비하지 않는다."""
    rels = set(r for r in rels or [] if r)
    if not rels:
        return False
    key = task_key(root, ctx.session_id, (task or {}).get("task_id") or ctx.task_id)
    for b in load(root, ctx.session_id).get("bypass") or []:
        if b.get("gate") == gate and b.get("paths") and b.get("task") == key and rels <= set(b["paths"]):
            return True
    return False


def bump_completion(root, session_id, task_key):
    st = load(root, session_id)
    n = int(st["completion_blocks"].get(task_key, 0)) + 1
    st["completion_blocks"][task_key] = n
    save(root, session_id, st)
    return n


def noticed_once(root, session_id, key):
    """같은 알림을 세션에서 한 번만. 처음이면 True."""
    st = load(root, session_id)
    if key in st["noticed"]:
        return False
    st["noticed"].append(key)
    save(root, session_id, st)
    return True


def note_writes(root, session_id, rels):
    """이 세션의 도구 쓰기 시각(파일별, time_ns) — 다른 세션의 긴 셸 명령이 실행 중 바뀐 파일을 제 것으로 세지 않게(opaque_watch._attribute).
    gates.opaque_stale_min보다 오래된 기록은 버린다(그보다 긴 실행 전 기록은 남지 않는다)."""
    import time
    from vmc_harness.core import config
    rels = [r for r in rels or [] if r]
    if not rels:
        return
    st = load(root, session_id)
    t = time.time_ns()
    cut = t - int(float(config.get("gates.opaque_stale_min", root)) * 60 * 1000 * 1000 * 1000)
    w = {k: v for k, v in (st.get("write_ns") or {}).items() if isinstance(v, int) and v >= cut}
    for r in rels:
        w[r] = t
    st["write_ns"] = w
    save(root, session_id, st)


def written_by_others(root, session_id, rels, since_ns):
    """rels 중 since_ns 이후 다른 세션이 도구로 쓴 파일."""
    want = set(rels or [])
    out = set()
    d = paths.cache_dir(root) / "gates"
    if not want or not d.is_dir():
        return out
    mine = _file(root, session_id).name
    for f in d.glob("session-*.json"):
        if f.name == mine:
            continue
        w = (read_json(f, None) or {}).get("write_ns") or {}
        out |= {r for r in want if isinstance(w.get(r), int) and w[r] >= since_ns}
    return out


def task_key(root, session_id, task_id=None):
    if task_id:
        return task_id
    t = current_task(root, session_id)
    if t and t.get("task_id"):
        return t["task_id"]
    return "S-%s" % (session_id or env_session_id() or "nosession")


def env_session_id():
    """에이전트 셸 안의 세션 id — 해석 순서의 원본은 task.state.env_session_id(Codex가 Claude보다 먼저)."""
    from vmc_harness.task.state import env_session_id as f
    return f()


def current_task(root, session_id):
    try:
        from vmc_harness.task import state as tstate
        return tstate.current(str(root), session_id)
    except (ImportError, AttributeError):
        return None
    except Exception:
        return None


def minutes_since(iso):
    t = parse_iso(iso)
    if not t:
        return None
    return (now() - t).total_seconds() / 60.0
