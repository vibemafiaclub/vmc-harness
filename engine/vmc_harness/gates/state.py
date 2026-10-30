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
    st = load(root, ctx.session_id)
    st["blocks"].append({"gate": gate, "gate_id": gate_id, "at": now_iso(), "fingerprint": fingerprint(ctx),
                         "event": ctx.event, "reason": reason, "proceeded": False})
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
                         "fingerprint": block["fingerprint"], "event": block.get("event"), "at": now_iso()})
    for b in st["blocks"]:
        if b["at"] == block["at"] and b["fingerprint"] == block["fingerprint"]:
            b["proceeded"] = True
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
