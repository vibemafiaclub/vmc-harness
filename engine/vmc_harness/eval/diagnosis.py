"""① 진단 정답지 — 시니어식 진단의 상위 k개 제안에 정답 권고가 몇 개 들어 있나 (spec 11, 12 미검증 #9).

데이터셋 `diagnosis_gold.json`:
    {"project": "<부트스트랩이 끝난 프로젝트 경로>", "home": "<그 프로젝트의 VMC_HARNESS_HOME, 선택>",
     "analysis_dir": "<사람 전수 태깅 폴더, 선택 — 고객 현장엔 없다>",
     "items": [{"id": "...", "label": "...", "any": [규칙, ...]}]}
    규칙(하나라도 맞으면 그 권고가 포함된 것):
      {"all": ["정규식", ...]}     제안 글(제목·막는 것·범위·조건·경로)에 정규식이 모두 맞는다
      {"kind": "protected"}        제안 종류
      {"condition_type": "db_direct"}  게이트 조건 type
판정은 결정적이다(LLM 판정 없음). 근거(evidence)·비용 글은 보지 않는다 — 인용 원문이 섞여 있어 제안 내용과 다르다.

진단은 지난번에 보인 제안을 다시 내지 않으므로(history) 실행마다 기록을 비운 채 돌리고, 끝나면 프로젝트의
diagnosis.json·history를 원래대로 돌려놓는다.
"""

import json
import re

from vmc_harness.core import config
from vmc_harness.core.jsonio import write_json
from vmc_harness.eval import common

TEXT_FIELDS = ("title", "blocks", "scope")


def proposal_text(p):
    parts = [str(p.get(k) or "") for k in TEXT_FIELDS]
    if p.get("condition"):
        parts.append(json.dumps(p["condition"], ensure_ascii=False))
    for k in ("protected_globs", "targets"):
        parts.extend(str(x) for x in p.get(k) or [])
    return "\n".join(parts)


def _rule_ok(rule, p, text):
    if "kind" in rule and p.get("kind") != rule["kind"]:
        return False
    if "condition_type" in rule and (p.get("condition") or {}).get("type") != rule["condition_type"]:
        return False
    for rx in rule.get("all") or []:
        if not re.search(rx, text, re.I):
            return False
    return bool(rule)


def score(proposals, items, top_k=None):
    """→ {"hits", "of", "matched": {item_id: [제안 번호]}, "missed": [item_id]}"""
    top_k = top_k or config.get("diagnose.top_k")
    props = list(proposals or [])[:top_k]
    matched = {}
    for it in items:
        nos = []
        for i, p in enumerate(props, 1):
            text = proposal_text(p)
            if any(_rule_ok(r, p, text) for r in it.get("any") or []):
                nos.append(p.get("no") or i)
        if nos:
            matched[it["id"]] = nos
    return {"hits": len(matched), "of": len(items), "matched": matched,
            "missed": [it["id"] for it in items if it["id"] not in matched]}


def _one_run(root, home, analysis_dir, progress):
    from vmc_harness.bootstrap import diagnose
    with common.home_env(home):
        diag_p = diagnose.diagnosis_path(root)
        hist_p = diagnose.history_path(root)
        backups = {}
        for p in (diag_p, hist_p):
            if p.is_file():
                backups[p] = p.read_bytes()
        try:
            write_json(hist_p, {"shown": [], "excluded": [], "accepted": []})
            return diagnose.run(root, analysis_dir=str(analysis_dir) if analysis_dir else None, progress=progress)
        finally:
            for p in (diag_p, hist_p):
                if p in backups:
                    p.write_bytes(backups[p])
                elif p.is_file():
                    p.unlink()


def run(dataset, runs=None, use_existing=True, progress=None):
    gold = common.load(dataset, "diagnosis_gold.json")
    items = gold.get("items") or []
    if not items:
        raise common.DatasetError("diagnosis_gold.json에 items 없음")
    root = common.expand(gold.get("project"), dataset)
    if not root or not (root / ".harness" / "config.json").is_file():
        return {"status": "no_data", "reason": "부트스트랩된 프로젝트 없음: %s" % root}
    home = common.expand(gold.get("home"), dataset)
    analysis_dir = common.expand(gold.get("analysis_dir"), dataset)
    runs = config.get("eval.diagnose_runs") if runs is None else runs
    say = progress or (lambda *_: None)
    rows = []
    if use_existing:
        with common.home_env(home):
            from vmc_harness.bootstrap import diagnose
            d = diagnose.load(root)
        if d and d.get("proposals"):
            rows.append(dict(score(d["proposals"], items), run="existing", generated_at=d.get("generated_at"),
                             model=d.get("model"), sec=d.get("sec"), tokens=d.get("tokens"),
                             titles=[p.get("title") for p in d["proposals"]]))
    for i in range(runs):
        say("① 진단 %d/%d 실행 (상한 %s초)" % (i + 1, runs, config.get("diagnose.budget_sec", root)))
        d = _one_run(root, home, analysis_dir, None)
        if d.get("status") != "ok":
            rows.append({"run": i + 1, "status": d.get("status"), "reason": d.get("reason") or d.get("checks"),
                         "hits": 0, "of": len(items)})
            continue
        rows.append(dict(score(d["proposals"], items), run=i + 1, generated_at=d.get("generated_at"),
                         model=d.get("model"), sec=d.get("sec"), tokens=d.get("tokens"),
                         titles=[p.get("title") for p in d["proposals"]]))
    scored = [r for r in rows if "matched" in r]
    if not scored:
        return {"status": "no_data", "reason": "점수 낼 진단 결과 없음", "runs": rows}
    coverage = {it["id"]: round(sum(1 for r in scored if it["id"] in r["matched"]) / len(scored), 3) for it in items}
    return {"status": "ok", "runs": rows, "items": [{"id": it["id"], "label": it.get("label")} for it in items],
            "metrics": {"hits_mean": round(sum(r["hits"] for r in scored) / len(scored), 3),
                        "of": len(items), "n_runs": len(scored)},
            "coverage": coverage, "analysis_dir_used": bool(analysis_dir)}
