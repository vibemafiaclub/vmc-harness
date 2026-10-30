"""로컬 원본 이벤트 (spec 09 `events.jsonl`). 팀 장부가 아니라 PC 안에만 남는다. 쓰는 시점 마스킹."""

from vmc_harness import KERNEL_VERSION
from vmc_harness.core.clock import now_iso
from vmc_harness.core.jsonio import append_jsonl, read_jsonl
from vmc_harness.core.mask import mask_obj
from vmc_harness.core.project import local_dir


def _masked(v):
    return mask_obj(v)


def log(root, kind, **fields):
    """kind 예: gate_fire, gate_block, proceed_anyway, gate_time, human_call, wait, card_search, rules_loaded"""
    rec = {"at": now_iso(), "kind": kind, "kernel_version": KERNEL_VERSION}
    rec.update(_masked(fields))
    append_jsonl(local_dir(root) / "events.jsonl", rec)
    return rec


def read(root, kind=None):
    rows = read_jsonl(local_dir(root) / "events.jsonl")
    return [r for r in rows if kind is None or r.get("kind") == kind]
