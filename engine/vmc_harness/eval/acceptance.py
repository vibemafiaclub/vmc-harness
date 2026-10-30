"""⑤ 제안 수용률 — 진단 제안 중 사람이 [추가]로 고른 비율 (spec 11). 명시적 선택이라 오탐 0인 지표.

데이터셋 `acceptance.json` (고객 운영 데이터 — 실습 이후): {"projects": [{"project": "<경로>", "home": "<VMC_HARNESS_HOME, 선택>"}]}
- 분모: 보인 제안 수(로컬 진단 기록 diagnose-history.json의 shown)
- 분자: [추가] 수 — 팀 장부의 조치 중 basis.ref == "diagnose"(장부가 원본). 로컬 기록 accepted는 대조용
"""

from vmc_harness.eval import common


def project_counts(root, home=None):
    from vmc_harness.bootstrap import diagnose
    from vmc_harness.core import ledger
    from vmc_harness.core.jsonio import read_json
    with common.home_env(home):
        hist = read_json(diagnose.history_path(root), {}) or {}
    acts = [a for a in ledger.list_actions(root) if a and (a.get("basis") or {}).get("ref") == "diagnose"]
    return {"shown": len(hist.get("shown") or []), "accepted_ledger": len(acts),
            "accepted_local": len(hist.get("accepted") or []), "excluded": len(hist.get("excluded") or [])}


def run(dataset, progress=None):
    cfg = common.load(dataset, "acceptance.json", required=False) or {}
    rows = []
    for p in cfg.get("projects") or []:
        root = common.expand(p.get("project"), dataset)
        if not root or not (root / ".harness" / "config.json").is_file():
            continue
        rows.append(dict(project_counts(root, common.expand(p.get("home"), dataset)), project=root.name))
    shown = sum(r["shown"] for r in rows)
    acc = sum(r["accepted_ledger"] for r in rows)
    if not shown or not acc and not any(r["excluded"] for r in rows):
        # 보인 제안이 없거나, 사람이 아직 아무것도 고르지 않았다 = 운영 데이터 없음
        return {"status": "no_data", "reason": "데이터 없음 — 진단 제안에 대한 사람의 [추가]·[제외] 기록이 없다", "rows": rows}
    return {"status": "ok", "metrics": {"rate": round(acc / shown, 3), "accepted": acc, "shown": shown}, "rows": rows}
