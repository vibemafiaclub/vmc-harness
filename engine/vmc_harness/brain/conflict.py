"""카드 충돌 (spec 04 §충돌).

카드·규칙이 지금 지시와 다르면 에이전트는 지시를 따르고 완료 보고에 한 줄을 쓰며 `vmc-harness card conflict <id>`를 부른다.
같은 카드가 brain.conflict_disable_count회 충돌하면 비활성 — 지침을 줄이는 방향이라 LLM 판정 입력을 허용(규칙 추가엔 안 씀).
충돌 기록은 브레인 레포(팀 공유)에 한 건 = 파일 하나. 충돌은 finding이 아니다(해석 신호).
"""

from vmc_harness.brain import store
from vmc_harness.core import config
from vmc_harness.core import ledger as core_ledger
from vmc_harness.core.clock import now_iso


def record(card_id, note=None, root=None):
    c = store.get_card(card_id)
    if c is None:
        raise KeyError(card_id)
    pc = None
    try:
        from vmc_harness.core.project import pc_id
        pc = pc_id()
    except Exception:
        pass
    store.add_conflict(card_id, note, pc)
    n = store.conflict_count(card_id)
    limit = config.get("brain.conflict_disable_count", root)
    res = {"card": card_id, "conflicts": n, "disabled": False}
    if n >= limit and c.get("status", "active") == "active":
        store.set_status(card_id, "disabled", reason="충돌 %d회" % n)
        res["disabled"] = True
        if root:
            res["actions"] = _close_actions(root, card_id, n)
    return res


def _close_actions(root, card_id, n):
    closed = []
    for a in core_ledger.list_actions(root):
        if a and (a.get("target") or {}).get("card") == card_id and a.get("status") in ("trial", "extended", "kept"):
            reason = "카드가 지시와 %d회 충돌해 비활성" % n
            core_ledger.update_action(root, a["action_id"], status="reverted", reverted_at=now_iso(),
                                      revert_reason=reason, reverted_by="conflict")
            core_ledger.write_verdict(root, {"action_id": a["action_id"], "period": [a.get("created_at"), now_iso()],
                                             "exposures": (a.get("trial") or {}).get("exposures", 0), "negatives": 0,
                                             "side": {"conflicts": n}, "result": "reverted", "reason": reason})
            closed.append(a["action_id"])
    return closed
