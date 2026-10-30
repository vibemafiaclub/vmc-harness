"""init 이후 실측 — 두 번째 진단부터 진단 입력과 리포트 "지난 진단 이후"에 쓴다 (spec 07 §정기 재진단).

모두 기계 집계다(판정·LLM 없음). 숫자·상태·id만(원문 없음, P9).
- 장부: 하네스가 넣은 조치(층·상태), 판정 결과, 노출 수
- 작업: 착수 선언 예측(P30~P80) 대비 실측 적중
- 게이트 발동 기록: 일일 집계의 게이트별 발동·막음·헛막기·시간
- 개입: init 이후 AI 대화로 기준선과 같은 정의(bootstrap.baseline.compute)로 다시 잰 값 — "하네스 전 / 지난 진단 / 지금"
- 진단 제안: 보인 수·[추가]·[제외]
"""

from vmc_harness.bootstrap import baseline as bbaseline
from vmc_harness.core import ledger, paths
from vmc_harness.core.clock import now, parse_iso
from vmc_harness.core.jsonio import read_json

# 리포트·진단이 비교하는 기준선 지표(같은 정의 — baseline.DEFINITIONS)
COMPARE = ("interventions_per_session", "interventions_per_10_tool_calls", "ai_questions_per_session",
           "recheck_after_done_ratio_keyword", "tool_calls_per_session")


def init_at(root):
    cfg = read_json(paths.harness_dir(root) / "config.json", {}) or {}
    return cfg.get("created_at")


def _after(at, since):
    if not since:
        return True
    a, s = parse_iso(at) if isinstance(at, str) else None, parse_iso(since)
    if not a or not s:
        return False
    if a.tzinfo is None:
        a = a.astimezone()
    if s.tzinfo is None:
        s = s.astimezone()
    return a >= s


def pick(b):
    """기준선 dict → 비교 지표만(숫자)."""
    if not isinstance(b, dict):
        return None
    out = {"sessions": (b.get("sessions") or {}).get("main_with_prompts"), "human_prompts": b.get("human_prompts")}
    for k in COMPARE:
        v = b.get(k)
        if k == "recheck_after_done_ratio_keyword" and v is None:
            v = b.get("recheck_after_done_ratio") if (b.get("recheck_after_done_ratio") or {}).get("method") == "keyword" else None
        if isinstance(v, dict):
            out[k] = {x: v.get(x) for x in ("median", "mean", "value", "numerator", "denominator") if v.get(x) is not None}
    return out


def _gate_name(root, gid):
    g = read_json(paths.harness_dir(root) / "gates" / ("%s.json" % gid), None) or {}
    return g.get("title") or g.get("name") or gid


def collect(root, sessions=None, prev=None):
    """init 이후 실측 dict. prev = 직전 진단 결과(diagnosis.json) — "지난 진단" 칸."""
    since = init_at(root)
    since_date = (since or "")[:10] or None
    out = {"since": since, "until": now().isoformat(), "previous_diagnosis_at": (prev or {}).get("generated_at")}
    acts = [a for a in ledger.list_actions(root) if a and _after(a.get("created_at"), since)]
    by = {}
    for a in acts:
        k = "%s/%s" % (a.get("layer"), a.get("status"))
        by[k] = by.get(k, 0) + 1
    out["actions"] = {"total": len(acts), "by_layer_status": by,
                      "reverted": sum(1 for a in acts if a.get("status") == "reverted"),
                      "kept": sum(1 for a in acts if a.get("status") == "kept"),
                      "trial": sum(1 for a in acts if a.get("status") == "trial"),
                      "items": [{"action_id": a.get("action_id"), "layer": a.get("layer"), "status": a.get("status"),
                                 "title": (a.get("title") or a.get("hypothesis") or "")[:160],
                                 "basis": (a.get("basis") or {}).get("type")} for a in acts]}
    vs = [v for v in ledger.list_verdicts(root) if v and _after(v.get("at") or v.get("created_at"), since)]
    vby = {}
    for v in vs:
        vby[v.get("result")] = vby.get(v.get("result"), 0) + 1
    out["verdicts"] = {"total": len(vs), "by_result": vby}
    try:
        from vmc_harness.ledger import metrics
        t = metrics.team(root, since_date)
        exp = sum(int(n) for m in metrics.load_all(root, since_date) for n in (m.get("exposures") or {}).values())
    except ImportError:
        t, exp = None, 0
    out["exposures"] = exp
    if t:
        gates = [{"gate_id": gid, "name": _gate_name(root, gid), "fired": g.get("fired", 0), "blocked": g.get("blocked", 0),
                  "false_block": g.get("false_block", 0), "sec": round(float(g.get("sec") or 0), 1)}
                 for gid, g in sorted(t["gates"].items())]
        out["gates"] = {"items": gates, "blocked": sum(g["blocked"] for g in gates),
                        "false_block": sum(g["false_block"] for g in gates), "fired": sum(g["fired"] for g in gates),
                        "sec": round(sum(g["sec"] for g in gates), 1)}
        out["interventions_hook"] = {"sessions": t["sessions"], "human_turns": t["human_turns"],
                                     "per_session": (t.get("reference") or {}).get("human_turns_per_session")}
        out["prediction"] = (t.get("reference") or {}).get("calibration")
    tasks = [x for x in ledger.read_tasks(root) if _after(x.get("started_at"), since)]
    n = hit = 0
    for x in tasks:
        act = x.get("actual") or {}
        pred = ((x.get("declare") or {}).get("predict") or {}).get("agent_sec") or {}
        if not act.get("measured") or not pred.get("p80"):
            continue
        n += 1
        if float(pred.get("p30") or 0) <= float(act.get("agent_sec") or 0) <= float(pred["p80"]):
            hit += 1
    out["tasks"] = {"total": len(tasks), "measured": n, "in_range": hit, "in_range_rate": round(hit / n, 2) if n else None}
    from vmc_harness.bootstrap import diagnose
    hist = read_json(diagnose.history_path(root), {}) or {}
    shown = [h for h in hist.get("shown") or [] if isinstance(h, dict)]
    added = [h for h in hist.get("accepted") or [] if isinstance(h, dict)]
    out["proposals"] = {"shown": len(shown), "added": len(added), "excluded": len(hist.get("excluded") or []),
                        "rate": round(len(added) / len(shown), 2) if shown else None}
    bl = ledger.read_baseline(root) or {}
    recent = [s for s in (sessions or []) if _after(s.get("started_at"), since)]
    previous = ((prev or {}).get("since_init") or {}).get("metrics", {}).get("now")
    corr = bl.get("guardian_correction") or {}
    if corr.get("status") == "corrected" and (prev or {}).get("generated_at", "") < corr.get("at", ""):
        previous = None  # 지난 진단의 "지금"은 자동 승인 심사 요청이 섞인 옛 읽기로 잰 값 — 같은 정의가 아니라 비교하지 않는다
    out["metrics"] = {"before": pick(bl.get("before")), "previous": previous,
                      "now": pick(bbaseline.compute(recent)) if recent else None}
    if corr.get("status") == "corrected":
        out["baseline_correction"] = corr
    return out


def has_data(m):
    if not m:
        return False
    return bool((m.get("actions") or {}).get("total") or (m.get("tasks") or {}).get("total")
                or (m.get("gates") or {}).get("fired") or ((m.get("metrics") or {}).get("now") or {}).get("sessions"))
