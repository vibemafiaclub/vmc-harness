"""회귀 세트 실행·기준선 비교·결과 파일 (spec 11).

결과: `<데이터셋>/results/<시각>.json`(+ `latest.json`, 사람이 읽는 `<시각>.md`). 기준선: `<데이터셋>/baseline.json`.
통과 = 기준선이 있는 모든 지표가 같거나 낫다(시간·토큰 지표만 `eval.overhead_tolerance_ratio` 잡음 허용).
데이터가 없거나 건너뛴 세트는 판정에서 빠진다(표에 그렇게 적는다).
"""

import os
import time
import traceback
from pathlib import Path

from vmc_harness import __version__
from vmc_harness.core import config, gitutil
from vmc_harness.core.clock import now, now_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.eval import SETS

# (세트, 지표, 방향, 잡음 허용 적용)
METRICS = [
    ("diagnosis", "hits_mean", "higher", False),
    ("classify", "precision", "higher", False),
    ("classify", "recall", "higher", False),
    ("render", "facts_ok_rate", "higher", False),
    ("render", "unknown_total", "lower", False),
    ("overhead", "small_sec", "lower", True),
    ("overhead", "mid_sec", "lower", True),
    ("overhead", "small_tokens", "lower", True),
    ("overhead", "mid_tokens", "lower", True),
    ("acceptance", "rate", "higher", False),
]
LABELS = {"diagnosis": "① 진단 정답지", "classify": "② 학습 후보 분류", "render": "③ 렌더링 대조",
          "overhead": "④ 오버헤드", "acceptance": "⑤ 제안 수용률"}


def _git_commit():
    root = Path(__file__).resolve().parents[3]
    rc, out, _ = gitutil.git(str(root), "rev-parse", "--short", "HEAD")
    rc2, st, _ = gitutil.git(str(root), "status", "--porcelain")
    return (out.strip() if rc == 0 else None), bool(st.strip()) if rc2 == 0 else None


def run_sets(dataset, sets=None, opts=None, progress=None):
    opts = opts or {}
    say = progress or (lambda *_: None)
    out = {}
    for name in sets or SETS:
        t0 = time.time()
        try:
            if name == "diagnosis":
                from vmc_harness.eval import diagnosis
                r = diagnosis.run(dataset, runs=opts.get("runs"), use_existing=opts.get("use_existing", True), progress=say)
            elif name == "classify":
                from vmc_harness.eval import classify
                r = classify.run(dataset, sample_prompts=opts.get("sample"), progress=say)
            elif name == "render":
                from vmc_harness.eval import render_set
                r = render_set.run(dataset, allow_llm=not opts.get("no_llm"), progress=say)
            elif name == "overhead":
                from vmc_harness.eval import overhead
                r = overhead.run(rounds=opts.get("rounds"), progress=say)
            elif name == "acceptance":
                from vmc_harness.eval import acceptance
                r = acceptance.run(dataset, progress=say)
            else:
                r = {"status": "error", "reason": "없는 세트: %s" % name}
        except Exception as e:  # 한 세트 실패가 나머지를 막지 않는다
            r = {"status": "error", "reason": "%s: %s" % (type(e).__name__, e), "trace": traceback.format_exc()[-2000:]}
        r["elapsed_s"] = round(time.time() - t0, 1)
        out[name] = r
        say("%s — %s (%.0f초)" % (LABELS.get(name, name), r.get("status"), r["elapsed_s"]))
    return out


def compare(sets, baseline):
    """→ (rows, passed). rows: {set, metric, value, baseline, direction, ok}"""
    tol = float(config.get("eval.overhead_tolerance_ratio"))
    rows, passed = [], True
    base_sets = (baseline or {}).get("sets") or {}
    for s, m, direction, noisy in METRICS:
        cur = sets.get(s) or {}
        val = (cur.get("metrics") or {}).get(m) if cur.get("status") == "ok" else None
        b = ((base_sets.get(s) or {}).get("metrics") or {}).get(m) if (base_sets.get(s) or {}).get("status") == "ok" else None
        row = {"set": s, "metric": m, "value": val, "baseline": b, "direction": direction, "ok": None}
        if val is None:
            row["note"] = "건너뜀(%s)" % (cur.get("status") or "실행 안 함")
        elif b is None:
            row["note"] = "기준선 없음"
        else:
            slack = abs(b) * tol if noisy else 0
            row["ok"] = val >= b - slack if direction == "higher" else val <= b + slack
            passed = passed and row["ok"]
        rows.append(row)
    return rows, passed


def run(dataset, sets=None, opts=None, progress=None):
    dataset = Path(dataset).expanduser()
    if not dataset.is_dir():
        raise FileNotFoundError("데이터셋 폴더 없음: %s" % dataset)
    commit, dirty = _git_commit()
    result = {"at": now_iso(), "engine_version": __version__, "git_commit": commit, "git_dirty": dirty,
              "eval_model": os.environ.get("VMC_HARNESS_EVAL_MODEL") or None, "dataset": str(dataset),
              "sets": run_sets(dataset, sets, opts, progress)}
    baseline = read_json(dataset / "baseline.json", None)
    rows, passed = compare(result["sets"], baseline)
    result["compare"] = {"baseline_at": (baseline or {}).get("at"), "rows": rows,
                         "passed": passed if baseline else None}
    return result


def save(dataset, result, set_baseline=False):
    """결과 파일을 쓰고 경로들을 돌려준다."""
    d = Path(dataset).expanduser() / "results"
    d.mkdir(parents=True, exist_ok=True)
    stamp = now().strftime("%y%m%d-%H%M%S")
    p = d / ("%s.json" % stamp)
    write_json(p, result)
    write_json(d / "latest.json", result)
    md = d / ("%s.md" % stamp)
    with md.open("w", encoding="utf-8", newline="\n") as f:   # write_text(newline=)은 3.10+
        f.write(summary_md(result))
    out = {"json": str(p), "md": str(md)}
    if set_baseline:
        b = Path(dataset).expanduser() / "baseline.json"
        write_json(b, result)
        out["baseline"] = str(b)
    return out


def _fmt(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        return ("%.3f" % v).rstrip("0").rstrip(".")
    return str(v)


def summary_md(result):
    L = ["# 회귀 세트 결과 %s" % result["at"], "",
         "- 엔진 %s · 커밋 %s%s · 평가 모델 %s" % (result["engine_version"], result.get("git_commit") or "-",
                                                   " (작업 트리 변경 있음)" if result.get("git_dirty") else "",
                                                   result.get("eval_model") or "운영 기본값"),
         "- 기준선: %s · 판정: %s" % (result["compare"].get("baseline_at") or "없음",
                                    {True: "통과", False: "실패", None: "기준선 없음"}[result["compare"]["passed"]]),
         "", "| 세트 | 지표 | 값 | 기준선 | 판정 |", "|---|---|---|---|---|"]
    for r in result["compare"]["rows"]:
        ok = {True: "같거나 나음", False: "나빠짐", None: r.get("note") or "-"}[r["ok"]]
        L.append("| %s | %s | %s | %s | %s |" % (LABELS.get(r["set"], r["set"]), r["metric"], _fmt(r["value"]),
                                              _fmt(r["baseline"]), ok))
    L.append("")
    for name, s in result["sets"].items():
        L.append("## %s — %s (%s초)" % (LABELS.get(name, name), s.get("status"), s.get("elapsed_s")))
        if s.get("reason"):
            L.append("- %s" % s["reason"])
        if name == "diagnosis" and s.get("runs"):
            for r in s["runs"]:
                L.append("- 실행 %s: %s/%s 포함 %s · 모델 %s · %s초" % (r.get("run"), r.get("hits"), r.get("of"),
                                                               r.get("matched") or {}, r.get("model"), r.get("sec")))
        if name == "classify" and s.get("metrics"):
            m = s["metrics"]
            L.append("- %s: 세션 %d · 발화 %d · 예측 %d · 정답 %d · TP %d · D 없는 세션 중 잘못 짚은 세션 %d/%d" % (
                s.get("method"), m["sessions"], m["prompts"], m["pred"], m["gold"], m["tp"],
                m["neg_sessions_flagged"], m["neg_sessions"]))
        if name == "render" and s.get("rows"):
            for r in s["rows"]:
                L.append("- [%s] %s" % (r["id"], r.get("situation") or ""))
                L.append("  - 하네스: %s" % (r["text"] or "").replace("\n", " "))
                L.append("  - 독자 원문: %s" % (r.get("reference") or "").replace("\n", " "))
                L.append("  - 사실 보존 %s · 낯선 단어 %s" % ("예" if r["facts_ok"] else "아니오(%s)" % r["missing_facts"],
                                                       r["unknown_words"] or "없음"))
        if name == "overhead" and s.get("metrics"):
            L.append("- %s (소 작업 예산 %s초)" % (s["metrics"], s.get("budget_small_s")))
        L.append("")
    return "\n".join(L)
