"""노출 기록 (spec 04 §판정 — 판정 입력 중 하나).

노출 = 기계 기록만: 게이트 발동, 카드 검색 반환, 클렌징 변경 영역의 작업, (규칙) 규칙이 로드된 세션.
기록 시점에 시험 중인 조치(target이 맞는 trial·extended)를 찾아 action_ids로 남긴다 → 일일 집계 `exposures`.
판정은 집계의 exposures만 본다(같은 사실을 한 곳에서만 센다).

조치 target 규약(조치를 만드는 쪽이 이 키로 남긴다):
  {"gate_id": …} 게이트 / {"card": …} 브레인 카드 / {"area": "경로 접두"} 클렌징·코드 정리 / {"rule_id": …} 규칙
"""

from vmc_harness.core import events
from vmc_harness.core import ledger as core_ledger

OPEN = ("trial", "extended")


def _norm_path(p):
    if not p:
        return p
    p = str(p).replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def in_area(path, area):
    path, area = _norm_path(path), _norm_path(area).rstrip("/")
    if not path or not area:
        return False
    return path == area or path.startswith(area + "/")


def matching_actions(root, gate_id=None, card=None, area=None, rule_id=None, actions=None):
    out = []
    for a in actions if actions is not None else core_ledger.list_actions(root):
        if not a or a.get("status") not in OPEN:
            continue
        t = a.get("target") or {}
        if gate_id and t.get("gate_id") == gate_id:
            out.append(a["action_id"])
        elif card and t.get("card") == card:
            out.append(a["action_id"])
        elif rule_id and t.get("rule_id") == rule_id:
            out.append(a["action_id"])
        elif area and t.get("area") and in_area(area, t["area"]):
            out.append(a["action_id"])
    return out


def record(root, action_id=None, gate_id=None, card=None, area=None, rule_id=None, session_id=None):
    """노출 1회 기록. 이벤트는 로컬 events.jsonl(kind=exposure), 집계는 metrics.rollup이 만든다.
    맞는 시험 조치가 없어도 기록은 남긴다(카드 반환 수 등 참고 집계용)."""
    if not root:
        return None
    ids = [action_id] if action_id else []
    if gate_id or card or area or rule_id:
        ids += [x for x in matching_actions(root, gate_id, card, area, rule_id) if x not in ids]
    fields = {"action_ids": ids}
    for k, v in (("gate_id", gate_id), ("card", card), ("area", _norm_path(area) if area else None),
                 ("rule_id", rule_id), ("session_id", session_id)):
        if v:
            fields[k] = v
    return events.log(root, "exposure", **fields)
