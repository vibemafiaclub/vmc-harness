"""사람에게 보일 알림 저장 (spec 08 §브리핑). 로컬 원본 — PC 안.

level: must(꼭 알릴 일 — 기계 조건) | flow(흐름 — 모델 판단) | growth(성장)
한 번 보인 건 다시 안 보인다. [알림 끄기] = finding(button=mute) → 그 kind는 이후 add 안 됨.
"""

from vmc_harness import KERNEL_VERSION
from vmc_harness.core.clock import now_iso
from vmc_harness.core.ids import new_uuid
from vmc_harness.core.jsonio import append_jsonl, read_json, read_jsonl, write_json
from vmc_harness.core.project import local_dir


def _file(root):
    return local_dir(root) / "notices.jsonl"


def _state_file(root):
    return local_dir(root) / "notices-state.json"


def _state(root):
    return read_json(_state_file(root), {"shown": [], "muted_kinds": []})


def add(root, kind, level, text, area=None, refs=None, buttons=None):
    """kind: 알림 종류(예: gate_blocked, gate_disabled, action_reverted, protected_changed, harness_error, structure_change)"""
    st = _state(root)
    if kind in st["muted_kinds"]:
        return None
    rec = {"id": new_uuid()[:8], "kind": kind, "level": level, "text": text, "area": area,
           "refs": refs or {}, "buttons": buttons or ["자세히", "알림 끄기"], "at": now_iso(),
           "kernel_version": KERNEL_VERSION}
    append_jsonl(_file(root), rec)
    return rec


def pending(root, area=None):
    """아직 안 보인 알림. area가 있으면 그 영역(경로 접두)과 관계있는 것만."""
    st = _state(root)
    out = []
    for n in read_jsonl(_file(root)):
        if n["id"] in st["shown"] or n["kind"] in st["muted_kinds"]:
            continue
        if area and n.get("area") and not (area.startswith(n["area"]) or n["area"].startswith(area)):
            continue
        out.append(n)
    return out


def mark_shown(root, ids):
    st = _state(root)
    st["shown"] = sorted(set(st["shown"]) | set(ids))
    write_json(_state_file(root), st)


def mute(root, notice_id):
    """[알림 끄기] — finding(button=mute) + 그 종류 감소."""
    from vmc_harness.core import ledger
    notes = {n["id"]: n for n in read_jsonl(_file(root))}
    n = notes.get(notice_id)
    if not n:
        raise KeyError(notice_id)
    st = _state(root)
    st["muted_kinds"] = sorted(set(st["muted_kinds"]) | {n["kind"]})
    write_json(_state_file(root), st)
    return ledger.create_finding(root, "button", {"notice_kind": n["kind"]}, button="mute")


def unmute(root, kinds):
    """[알림 끄기] 되돌리기 — 그 종류를 다시 알린다(남은 finding은 그대로: 끈 사실은 사람 행동 기록)."""
    st = _state(root)
    st["muted_kinds"] = sorted(set(st["muted_kinds"]) - set(kinds))
    write_json(_state_file(root), st)
    return st
