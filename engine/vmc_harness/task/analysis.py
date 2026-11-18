"""예측 학습 루프 — 빗나간 원인을 기계로 정하고, 같은 영역·원인이 쌓이면 루프 티켓을 만든다(spec 13).

- 작업 하나에서 보는 것은 꼬리뿐: 실측 > P80. P30·P50 적중은 모아서 본다(measure.calibration)
- 원인은 LLM 없이 위에서부터 처음 맞는 것: requirement → cache(토큰만) → unknown(비슷한 작업 부족)
  → 단계 초과(explore·edit·verify·delegate, edit 초과 + 범위 밖이 많으면 structure) → predict(어느 단계도 평소의 1.5배 안)
- 조치 근거는 같은 영역·같은 원인 `predict_loop.miss_repeat`회. 한 번 빗나간 것으로는 아무것도 바꾸지 않는다
- 사람에게 주는 것(사용자 행동)은 주 1회 성장 요약 한 줄뿐(weekly_hint)
"""

from vmc_harness.core import config, ledger
from vmc_harness.core.clock import now_iso, parse_iso

STEP_PHASES = ("explore", "edit", "verify", "delegate")
TICKET_CAUSES = ("structure", "edit", "explore", "verify", "delegate")  # 루프가 레포를 고쳐 풀 수 있는 원인
PHASE_OF = {"structure": "edit"}
REQUIREMENT_SHARE = 0.5   # 재작업이 초과분의 이 비율 이상이면 요구 변경
CACHE_SHARE = 0.5         # 캐시 재생성이 토큰 초과분의 이 비율 이상이면 캐시 만료
NORMAL_RATIO = 1.5        # 모든 단계가 비슷한 작업 중앙값의 이 배 안이면 "예측이 낮았다"
OUT_OF_SCOPE_SHARE = 0.5  # 바뀐 파일 중 선언 범위 안 비율이 이보다 낮으면 구조가 번진 것

LABEL = {"requirement": "요구 변경", "cache": "캐시 만료", "structure": "구조 경직(바꾸려던 곳이 다른 모듈로 번짐)",
         "edit": "구현", "explore": "조사", "verify": "검증", "delegate": "위임", "predict": "예측이 낮았다",
         "unknown": "원인 미정"}


def _median(xs):
    xs = sorted(float(x) for x in xs)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2.0


def areas(root, task):
    """이번 작업이 바꾼 파일의 구조 지도 모듈(07). 바뀐 파일을 모르면 선언 범위로."""
    from vmc_harness.loop import detect
    files = (task.get("scope_cache") or {}).get("files") or (task.get("declare") or {}).get("scope") or []
    out = set()
    for f in files[:200]:
        try:
            out.add(detect.module_of(root, f) or "")
        except Exception:
            continue
    return sorted(out)


def _phase_medians(root, task, tasks):
    from vmc_harness.task import measure
    pool = [t for t in tasks if t.get("task_id") != task.get("task_id") and t.get("phases")]
    sims = [r for _, r in measure.similar(root, task.get("declare") or {}, tasks=pool)]
    if len(sims) < config.get("predict_loop.similar_min", root):
        return None, len(sims)
    return {p: _median((r.get("phases") or {}).get(p) or 0 for r in sims) for p in STEP_PHASES}, len(sims)


def _cause(task, metric, overrun, ph, med, excess):
    act = task.get("actual") or {}
    pred = (task.get("declare") or {}).get("predict") or {}
    p80_sec = (pred.get("agent_sec") or {}).get("p80")
    time_over = max(0.0, float(act.get("agent_sec") or 0) - float(p80_sec or 0)) if p80_sec is not None else 0.0
    if int(task.get("declare_rev") or 1) > 1 or (ph and time_over and float(ph.get("rework") or 0) >= REQUIREMENT_SHARE * time_over):
        return "requirement"
    if metric == "tokens":
        rec = int(((act.get("tokens_detail") or {}).get("cache_recreate")) or 0)
        if overrun > 0 and rec >= CACHE_SHARE * overrun:
            return "cache"
    if not ph or med is None:
        return "unknown"
    ratios = [float(ph.get(p) or 0) / med[p] for p in STEP_PHASES if med.get(p)]
    unseen = [p for p in STEP_PHASES if not med.get(p) and float(ph.get(p) or 0) > 0]
    if not unseen and (not ratios or max(ratios) <= NORMAL_RATIO):
        return "predict"
    top = max(STEP_PHASES, key=lambda p: excess.get(p) or 0)
    if top == "edit":
        touch = (task.get("scope_cache") or {}).get("touch") or task.get("touch") or {}
        ins = touch.get("in_scope")
        if ins is not None and float(ins) < OUT_OF_SCOPE_SHARE:
            return "structure"
    return top


def analyze(root, task, tasks):
    """실측된 작업 하나 → analysis(09). 실측이 없으면 None."""
    act = task.get("actual") or {}
    if not act.get("measured"):
        return None
    pred = (task.get("declare") or {}).get("predict") or {}
    ph = task.get("phases") or None
    med, n_sim = _phase_medians(root, task, tasks)
    excess = {p: round(float((ph or {}).get(p) or 0) - med[p], 1) for p in STEP_PHASES} if med and ph else None
    out = {"over_p80": {}, "cause": {}, "excess": excess, "similar_n": n_sim, "area": areas(root, task),
           "late_explore": bool(ph and (task.get("phases_detail") or {}).get("explore_late", 0)
                                >= 0.5 * float(ph.get("explore") or 0) > 0),
           "analyzed_at": now_iso()}
    for metric in ("agent_sec", "tokens"):
        p80 = (pred.get(metric) or {}).get("p80")
        a = act.get(metric)
        over = a is not None and p80 is not None and float(a) > float(p80)
        out["over_p80"][metric] = over
        out["cause"][metric] = _cause(task, metric, float(a) - float(p80), ph, med, excess or {}) if over else None
    return out


def _key(area, cause):
    return "%s|%s" % (area or "-", cause)


def _open_keys(root):
    keys = set()
    for c in ledger.list_candidates(root):
        if c and c.get("kind") == "predict" and c.get("status") in ("open", "proposed", "prepared"):
            keys.add(c.get("predict_key"))
    for a in ledger.list_actions(root):
        if a and (a.get("basis") or {}).get("type") == "predict_miss" and a.get("status") in ("trial", "prepared", "extended"):
            keys.add((a.get("predict") or {}).get("key"))
    return keys


def make_tickets(root, tasks):
    """같은 영역·같은 원인 빗나감이 miss_repeat번 쌓였고 열린 티켓·시험 중 조치가 없으면 루프 티켓(kind=predict)."""
    need = config.get("predict_loop.miss_repeat", root)
    groups = {}
    for t in tasks:
        an = t.get("analysis") or {}
        cause = (an.get("cause") or {}).get("agent_sec") or (an.get("cause") or {}).get("tokens")
        if cause not in TICKET_CAUSES:
            continue
        for area in an.get("area") or [""]:
            groups.setdefault(_key(area, cause), []).append(t)
    open_keys = _open_keys(root)
    made = []
    for key, ts_ in sorted(groups.items()):
        if len(ts_) < need or key in open_keys:
            continue
        area, cause = key.split("|", 1)
        phase = PHASE_OF.get(cause, cause)
        base = _median(((t.get("analysis") or {}).get("excess") or {}).get(phase) or 0 for t in ts_)
        files = []
        for t in ts_:
            for f in (t.get("scope_cache") or {}).get("files") or []:
                if f not in files and (not area or area == "-" or f.startswith(area)):
                    files.append(f)
        summary = "예측 회고: %s에서 %s로 %d건 빗나감" % (area or "루트", LABEL[cause], len(ts_))
        rec = ledger.create_candidate(root, "predict", summary)
        rec = ledger.update_candidate(root, rec["candidate_id"], predict_key=key, cause=cause, area=area, phase=phase,
                                      task_ids=[t.get("task_id") for t in ts_], baseline_excess=base,
                                      intents=[str((t.get("declare") or {}).get("intent") or "")[:120] for t in ts_][:5],
                                      targets=files[:20], anchor={"path": files[0]} if files else {})
        made.append(rec)
    return made


def run(root):
    """upkeep(LLM 없음)이 부른다: 실측됐는데 analysis가 없는 작업을 분석해 장부에 다시 쓰고, 티켓을 만든다."""
    if not config.get("predict_loop.enabled", root):
        return {"enabled": False}
    from vmc_harness.task import lifecycle, state as st
    tasks = [t for t in st.all_tasks(root, archived=True) if t.get("ended_at") and (t.get("actual") or {}).get("measured")]
    n = 0
    for t in tasks:
        if t.get("analysis"):
            continue
        an = analyze(root, t, tasks)
        if an is None:
            continue
        t["analysis"] = an
        st.save(root, t)
        lifecycle.publish(root, t, "ended")
        n += 1
    made = make_tickets(root, tasks)
    return {"enabled": True, "analyzed": n, "tickets": len(made)}


HINTS = {"requirement": "요구가 작업 중에 바뀌어 빗나간 작업 %d건 — 착수 한 줄의 '끝'이 틀렸으면 그때 한마디로 바로잡으면 됩니다",
         "cache": "자리를 오래 비운 뒤 이어 간 작업 %d건에서 문맥을 다시 쓰느라 토큰이 늘었습니다"}


def weekly_hint(tasks, start, end):
    """주 1회 성장 요약의 사용자 행동 한 줄(가장 많은 원인 하나). 없으면 None."""
    cnt = {}
    for t in tasks:
        at = parse_iso(t.get("ended_at"))
        if not at or not (start <= at < end):
            continue
        for c in ((t.get("analysis") or {}).get("cause") or {}).values():
            if c in HINTS:
                cnt[c] = cnt.get(c, 0) + 1
                break
    if not cnt:
        return None
    c = max(cnt, key=cnt.get)
    return HINTS[c] % cnt[c]
