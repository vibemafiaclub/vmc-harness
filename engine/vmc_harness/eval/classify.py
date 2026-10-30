"""② 학습 후보 분류 — 자가 점검의 학습 후보 추출이 D(AI 결함 복구) 발화를 얼마나 맞게 짚나 (spec 11).

데이터셋 `classify.json`:
    {"prompts": "<prompts.jsonl — kind=main 행이 사람 발화>", "tags": "<tag_*.md glob — 세션별 표 | sid | agent | 날짜 | H | A | B | C | D |>"}
분류 함수는 운영과 같은 것(`ledger.selfcheck.extract` + `validate`)을 쓴다. 학습 후보가 나온 발화 = D로 예측.

정답은 0주차 전수 태깅의 **세션별 D 개수**다(발화별 정답은 없다). 그래서 세션마다 개수로 대조한다:
TP = Σ min(예측 수, 정답 수), 정밀도 = TP / Σ예측, 재현율 = TP / Σ정답. 같은 세션 안에서 다른 발화를 짚어도 맞은 것으로
세므로 발화 단위 값의 상한 근사다(결과에 그렇게 적는다).
표본: 세션 id 해시 순서(결정적)로 D가 있는 세션과 없는 세션을 번갈아 담아 발화 수가 상한에 닿을 때까지.
"""

import glob
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from vmc_harness.core import config
from vmc_harness.eval import common

_ROW = re.compile(r"^\|\s*\**([0-9a-f]{8})\**\s*\|\s*([^|]+?)\s*\|\s*[^|]*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|\s*(\d+)\s*\|")


def parse_tags(paths):
    """→ {sid8: {"H","A","B","C","D","agent"}}"""
    out = {}
    for p in paths:
        for line in Path(p).read_text(encoding="utf-8").splitlines():
            m = _ROW.match(line.strip())
            if not m:
                continue
            sid8, agent, h, a, b, c, d = m.groups()
            out[sid8] = {"agent": agent, "H": int(h), "A": int(a), "B": int(b), "C": int(c), "D": int(d)}
    return out


def load_prompts(path):
    """→ {sid: [발화 원문, i 순서]} (kind=main만)."""
    by = {}
    with Path(path).open("r", encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("kind") != "main":
                continue
            by.setdefault(r["sid"], []).append((int(r.get("i") or 0), r.get("text") or ""))
    return {sid: [t for _, t in sorted(v)] for sid, v in by.items()}


def join(tags, prompts):
    """태그 표 세션 ↔ 발화. 발화 수가 표의 H와 다른 세션은 뺀다(정답 개수를 믿을 수 없다). → [(sid, gold_D, [발화])], 뺀 수"""
    by8 = {}
    for sid in prompts:
        by8.setdefault(sid[:8], []).append(sid)
    out, dropped = [], 0
    for sid8, t in tags.items():
        sids = by8.get(sid8) or []
        if len(sids) != 1 or len(prompts[sids[0]]) != t["H"]:
            dropped += 1
            continue
        out.append((sids[0], t["D"], prompts[sids[0]]))
    return out, dropped


def _h(s):
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def sample(sessions, max_prompts, session_max):
    pos = sorted([s for s in sessions if s[1] > 0 and len(s[2]) <= session_max], key=lambda s: _h(s[0]))
    neg = sorted([s for s in sessions if s[1] == 0 and len(s[2]) <= session_max], key=lambda s: _h(s[0]))
    out, n = [], 0
    while (pos or neg) and n < max_prompts:
        for src in (pos, neg):
            if src and n < max_prompts:
                s = src.pop(0)
                out.append(s)
                n += len(s[2])
    return out


def classify_session(root, texts):
    """운영과 같은 경로로 학습 후보 추출 → {"pred": D로 본 발화 수, "indices", "corrections", "tokens", "sec", "ok"}"""
    from vmc_harness.core.mask import mask
    from vmc_harness.ledger import selfcheck
    lim = config.get("selfcheck.prompt_chars", root)
    prompts = [mask(t)[:lim] for t in texts][-config.get("selfcheck.max_prompts", root):]
    t0 = time.time()
    out, res = selfcheck.extract(root, prompts)
    sec = round(time.time() - t0, 1)
    if out is None:
        return {"ok": False, "pred": 0, "indices": [], "corrections": 0, "tokens": 0, "sec": sec}
    corrections, learnings, _ = selfcheck.validate(out, prompts)
    idx = sorted({int(l["prompt_index"]) for l in learnings})
    return {"ok": True, "pred": len(idx), "indices": idx, "corrections": corrections,
            "tokens": int((res or {}).get("tokens") or 0), "sec": sec}


def metrics(rows):
    pred = sum(r["pred"] for r in rows)
    gold = sum(r["gold"] for r in rows)
    tp = sum(min(r["pred"], r["gold"]) for r in rows)
    return {"precision": round(tp / pred, 3) if pred else None, "recall": round(tp / gold, 3) if gold else None,
            "tp": tp, "pred": pred, "gold": gold,
            "sessions": len(rows), "prompts": sum(r["prompts"] for r in rows),
            "neg_sessions_flagged": sum(1 for r in rows if r["gold"] == 0 and r["pred"] > 0),
            "neg_sessions": sum(1 for r in rows if r["gold"] == 0)}


def run(dataset, sample_prompts=None, progress=None):
    from vmc_harness.core import llm
    cfg = common.load(dataset, "classify.json")
    prompts_p = common.expand(cfg.get("prompts"), dataset)
    tag_paths = sorted(glob.glob(str(common.expand(cfg.get("tags"), dataset))))
    if not prompts_p or not prompts_p.is_file() or not tag_paths:
        return {"status": "no_data", "reason": "prompts.jsonl 또는 tag_*.md 없음"}
    if not llm.available():
        return {"status": "skipped", "reason": "claude CLI 없음"}
    say = progress or (lambda *_: None)
    sessions, dropped = join(parse_tags(tag_paths), load_prompts(prompts_p))
    n = sample_prompts or config.get("eval.classify_sample_prompts")
    picked = sample(sessions, n, config.get("eval.classify_session_max_prompts"))
    say("② 세션 %d개 · 발화 %d개 분류 (전체 대조 가능 세션 %d개, 발화 수 불일치로 뺀 세션 %d개)" % (
        len(picked), sum(len(s[2]) for s in picked), len(sessions), dropped))
    rows = []
    with common.temp_project() as (root, home), common.env(VMC_HARNESS_HOME=str(home), VMC_HARNESS_NESTED=None):
        def work(s):
            r = classify_session(root, s[2])
            return dict(r, sid=s[0][:8], gold=s[1], prompts=len(s[2]))
        with ThreadPoolExecutor(max_workers=config.get("eval.classify_workers")) as ex:
            rows = list(ex.map(work, picked))
    ok = [r for r in rows if r["ok"]]
    if not ok:
        return {"status": "error", "reason": "LLM 호출이 모두 실패", "rows": rows}
    m = metrics(ok)
    m["failed_sessions"] = len(rows) - len(ok)
    return {"status": "ok", "metrics": m, "rows": rows,
            "method": "세션별 D 개수 대조(발화 단위 상한 근사)", "population": {
                "sessions": len(sessions), "prompts": sum(len(s[2]) for s in sessions),
                "gold_D": sum(s[1] for s in sessions), "dropped_sessions": dropped},
            "tokens": sum(r["tokens"] for r in rows)}
