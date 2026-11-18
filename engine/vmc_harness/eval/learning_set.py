"""⑥ 학습 발화 라벨 — 자가 점검 학습이 답·선택·승인을 정정으로 읽지 않는가 (spec 04·11).

데이터셋 `learning.json`:
    {"rows": "<rows.jsonl>"}
    rows.jsonl 한 줄 = {"id", "quote"(학습에 쓰인 사람 발화 일부), "human"(사람 메시지 전체, 없으면 quote),
                        "prevA"(직전 AI 응답 끝, 없으면 null), "verdict": "ok|overgen|wrong"(사람·검수 라벨)}
분류 함수는 운영과 같은 것(`ledger.selfcheck.extract` + `validate`)을 쓴다. 발화 eval.learning_batch개를 한 세션처럼
묶어(발화마다 자기 직전 AI 응답을 붙여) 넘기고, 그 발화에서 기계 검사를 통과한 학습이 나왔는지 본다.

지표: wrong_removed = 오학습(wrong) 발화 중 학습이 안 나온 비율(높을수록 좋다),
      ok_lost = 정상(ok) 발화 중 학습이 안 나온 비율(낮을수록 좋다), overgen_removed(참고),
      ask_user_ratio = 나온 학습 판단 중 "확인·묻기" 처방 비율(낮을수록 좋다).
LLM 출력이 흔들리므로 eval.learning_runs회 돌려 지표마다 중앙값을 낸다.
"""

import json
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from vmc_harness.core import config
from vmc_harness.eval import common

ASK_LIKE = re.compile(r"(확인|물어|묻|여쭤)")


def load_rows(path):
    rows = []
    with open(str(path), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                r = json.loads(line)
                if r.get("verdict") in ("ok", "overgen", "wrong") and (r.get("human") or r.get("quote")):
                    rows.append(r)
    return rows


def batches(rows, n):
    return [rows[i:i + n] for i in range(0, len(rows), n)]


def run_batch(root, rows):
    """한 묶음 → {id: {"learned": bool, "judgments": [..]}}, tokens, ok"""
    from vmc_harness.core.mask import mask
    from vmc_harness.ledger import selfcheck
    lim = config.get("selfcheck.prompt_chars", root)
    n = config.get("selfcheck.ai_context_chars", root)
    prompts = [mask(r.get("human") or r["quote"])[:lim] for r in rows]
    contexts = [mask(selfcheck.ai_tail(r.get("prevA") or "", n)) for r in rows]
    out, res = selfcheck.extract(root, prompts, contexts=contexts)
    if out is None:
        return None, int((res or {}).get("tokens") or 0)
    _, learnings, _ = selfcheck.validate(out, prompts)
    got = {r["id"]: {"learned": False, "judgments": []} for r in rows}
    for l in learnings:
        i = int(l["prompt_index"])
        if 0 <= i < len(rows):
            g = got[rows[i]["id"]]
            g["learned"] = True
            g["judgments"].append(l.get("judgment") or "")
    return got, int((res or {}).get("tokens") or 0)


def metrics(rows, got):
    def rate(verdict, learned):
        ids = [r["id"] for r in rows if r["verdict"] == verdict and r["id"] in got]
        if not ids:
            return None, 0, 0
        k = sum(1 for i in ids if got[i]["learned"] == learned)
        return round(k / len(ids), 3), k, len(ids)
    wr, wk, wn = rate("wrong", False)
    ol, ok_, on = rate("ok", False)
    gr, gk, gn = rate("overgen", False)
    js = [j for v in got.values() for j in v["judgments"]]
    ask = sum(1 for j in js if ASK_LIKE.search(j))
    return {"wrong_removed": wr, "wrong_removed_n": "%d/%d" % (wk, wn),
            "ok_lost": ol, "ok_lost_n": "%d/%d" % (ok_, on),
            "overgen_removed": gr, "overgen_removed_n": "%d/%d" % (gk, gn),
            "ask_user_ratio": round(ask / len(js), 3) if js else None, "learned": len(js),
            "rows": sum(1 for r in rows if r["id"] in got)}


def run(dataset, runs=None, progress=None):
    from vmc_harness.core import llm
    cfg = common.load(dataset, "learning.json", required=False)
    if not cfg:
        return {"status": "no_data", "reason": "learning.json 없음"}
    p = common.expand(cfg.get("rows"), dataset)
    if not p or not p.is_file():
        return {"status": "no_data", "reason": "rows.jsonl 없음"}
    if not llm.available():
        return {"status": "skipped", "reason": "claude CLI 없음"}
    say = progress or (lambda *_: None)
    rows = load_rows(p)
    k = runs or config.get("eval.learning_runs")
    per_run, tokens = [], 0
    for run_i in range(k):
        t0 = time.time()
        got = {}
        with common.temp_project() as (root, home), common.env(VMC_HARNESS_HOME=str(home), VMC_HARNESS_NESTED=None):
            with ThreadPoolExecutor(max_workers=config.get("eval.classify_workers")) as ex:
                for g, t in ex.map(lambda b: run_batch(root, b), batches(rows, config.get("eval.learning_batch"))):
                    tokens += t
                    if g:
                        got.update(g)
        m = metrics(rows, got)
        kinds = sorted({r.get("kind") for r in rows if r.get("kind")})
        if len(kinds) > 1:
            m["by_kind"] = {kd: metrics([r for r in rows if r.get("kind") == kd], got) for kd in kinds}
        m["sec"] = round(time.time() - t0, 1)
        per_run.append(m)
        say("⑥ 회차 %d: 오학습 제거 %s · 정상 손실 %s · 확인 처방 %s" % (
            run_i + 1, m["wrong_removed_n"], m["ok_lost_n"], m["ask_user_ratio"]))
    med = {}
    for key in ("wrong_removed", "ok_lost", "overgen_removed", "ask_user_ratio"):
        vals = [m[key] for m in per_run if m.get(key) is not None]
        med[key] = statistics.median(vals) if vals else None
    return {"status": "ok", "metrics": med, "runs": per_run, "tokens": tokens, "rows": len(rows),
            "method": "발화 단위 라벨 대조(%d회 중앙값)" % k}
