"""카드 승격 (spec 04 §승격, 01 P1·P2).

사람이 명시 요청해 만든 카드(origin explicit_request)만, brain.promote_min_uses회 이상 쓰이고(팀 합산 카드 반환 수)
명시적 부정이 0이면 게이트·구조로 올리는 조치를 만든다. 학습 후보 출처 카드는 카드 밖으로 못 나간다(메타 게이트도 막는다).

게이트 본체 생성은 gates 영역 몫 — `vmc_harness.gates.generate.from_request(root, quote, source, promoted_from)`가
있으면 위임한다. 없으면 승격 제안을 `.harness/cache/promotions.json`에 쌓고 흐름 알림 한 줄(사람 승인 요청 아님, P4).
"""

from vmc_harness.brain import store
from vmc_harness.core import config, notices, paths
from vmc_harness.core import ledger as core_ledger
from vmc_harness.core.clock import now_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.ledger import judge, metrics


def eligible(root):
    rows = metrics.load_all(root)
    findings = core_ledger.list_findings(root)
    need = config.get("brain.promote_min_uses", root)
    out = []
    for a in core_ledger.list_actions(root):
        if not a or a.get("layer") != "card" or a.get("promoted_to"):
            continue
        if (a.get("basis") or {}).get("type") != "explicit_request" or not a.get("human_requested"):
            continue
        if a.get("status") not in ("trial", "extended", "kept"):
            continue
        cid = (a.get("target") or {}).get("card")
        card = store.get_card(cid) if cid else None
        if not card or card.get("origin") != "explicit_request" or card.get("status", "active") != "active":
            continue
        uses = metrics.card_uses(root, cid, None, rows)
        hard, fb = judge.negatives(a, findings)
        if uses >= need and hard + fb == 0:
            out.append((a, card, uses))
    return out


def _delegate(root, action, card):
    try:
        from vmc_harness.gates import generate
        fn = generate.from_request
    except (ImportError, AttributeError):
        return None
    return fn(root, quote=(action.get("basis") or {}).get("quote") or card.get("quote"),
              source={"card": card["id"], "action_id": action["action_id"]}, promoted_from=card["id"])


def check(root):
    done = []
    for a, card, uses in eligible(root):
        res = None
        try:
            res = _delegate(root, a, card)
        except Exception as e:
            res = {"error": str(e)[:200]}
        if res and not (isinstance(res, dict) and res.get("error")):
            core_ledger.update_action(root, a["action_id"], promoted_to=res if isinstance(res, dict) else {"ref": str(res)})
        else:
            p = paths.cache_dir(root) / "promotions.json"
            rows = read_json(p, []) or []
            rows.append({"card": card["id"], "action_id": a["action_id"], "uses": uses, "at": now_iso(),
                         "quote": (a.get("basis") or {}).get("quote")})
            write_json(p, rows)
            core_ledger.update_action(root, a["action_id"], promoted_to={"pending": True, "at": now_iso()})
            notices.add(root, "card_promotable", "flow",
                        "판단 '%s'가 %d번 쓰였고 되돌린 적이 없어요. 게이트로 올릴 수 있어요." % (card.get("title"), uses),
                        refs={"card": card["id"], "action_id": a["action_id"]})
        done.append(card["id"])
    return done
