"""③ 렌더링 대조 — 하네스가 사람에게 보일 글이 사실을 지키고 독자에게 낯선 단어가 없나 (spec 11).

데이터셋 `render_cases.json`:
    {"reader": {"name": "...", "prompts": "<독자 본인 발화 prompts.jsonl — 독자 프로필을 만든다>"} 또는 {"profile": "<프로필 json>"},
     "cases": [{"id", "situation", "facts": {기계 기록}, "draft": "하네스 초안(선택)", "reference": "독자가 같은 상황에서 실제로 쓴 문장"}]}
지표: 사실 보존 100%, 낯선 단어 0(render.engine.evaluate). 자연스러움은 사람이 결과의 text·reference를 나란히 읽어 판단한다.
"""

import json
from pathlib import Path

from vmc_harness.eval import common


def _reader(cfg, dataset, root):
    from vmc_harness.render import lexicon, profile
    r = cfg.get("reader") or {}
    if r.get("profile"):
        return json.loads(common.expand(r["profile"], dataset).read_text(encoding="utf-8"))
    p = common.expand(r.get("prompts"), dataset)
    if not p or not Path(p).is_file():
        return {}
    texts = []
    with Path(p).open("r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("kind", "main") == "main" and row.get("text"):
                texts.append(row["text"])
    return profile.build(texts, groups=lexicon.groups(root), name=r.get("name"), root=root)


def run(dataset, allow_llm=True, progress=None):
    from vmc_harness.render import engine
    cfg = common.load(dataset, "render_cases.json")
    cases = cfg.get("cases") or []
    if not cases:
        return {"status": "no_data", "reason": "render_cases.json에 cases 없음"}
    say = progress or (lambda *_: None)
    with common.temp_project() as (root, home), common.env(VMC_HARNESS_HOME=str(home), VMC_HARNESS_NESTED=None):
        reader = _reader(cfg, dataset, root)
        say("③ 렌더링 %d쌍 (독자 프로필 어휘 %d개, LLM %s)" % (len(cases), len(reader.get("vocab") or {}),
                                                       "켬" if allow_llm else "끔"))
        out = engine.evaluate(str(root), reader, cases, allow_llm=allow_llm)
    s = out["summary"]
    by_id = {c.get("id"): c for c in cases}
    rows = []
    for r in out["cases"]:
        c = by_id.get(r.get("id")) or {}
        rows.append({"id": r.get("id"), "situation": c.get("situation"), "draft": c.get("draft"), "text": r["text"],
                     "reference": r.get("reference"), "facts_ok": r["facts_ok"], "missing_facts": r["missing_facts"],
                     "unknown_words": r["unknown_words"], "rewritten": r["rewritten"]})
    return {"status": "ok", "metrics": {"facts_ok_rate": s["facts_ok_rate"], "unknown_total": s["unknown_total"],
                                        "unknown_zero_rate": s["unknown_zero_rate"], "n": s["n"],
                                        "llm_calls": s["llm_calls"]},
            "rows": rows, "llm": allow_llm}
