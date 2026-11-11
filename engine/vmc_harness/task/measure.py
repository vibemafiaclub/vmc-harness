"""실측·보정 점수·참고 지표 (spec 03 §실측과 보정).

- 보정 점수: 실측이 P30/P50/P80 안에 든 비율이 30/50/80%에서 얼마나 어긋나는가
- 실측 > P80 건수, 선언 대비 실제 건드린 비율
- 전부 **참고 지표**(finding 아님). 작업 중에는 에이전트에게 보이지 않는다(01 P5) — 착수 선언 출력(declare_context)에만
"""

import math

from vmc_harness.core import config, ledger
from vmc_harness.core.clock import now
from vmc_harness.core.jsonio import read_json, write_json

LEVELS = (("p30", 0.3), ("p50", 0.5), ("p80", 0.8))


def _bigrams(text):
    t = "".join(str(text or "").lower().split())
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) > 1 else ({t} if t else set())


def _parts(scope):
    out = set()
    for s in scope or []:
        for p in str(s).replace("\\", "/").split("/"):
            p = p.strip()
            if p and p not in (".", "*", "**"):
                out.add(p.lower())
    return out


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / float(len(a | b))


def score(declare_a, declare_b):
    """범위 겹침과 의도 겹침의 평균(둘 다 0~1)."""
    s_scope = _jaccard(_parts(declare_a.get("scope")), _parts(declare_b.get("scope")))
    s_intent = _jaccard(_bigrams(declare_a.get("intent")), _bigrams(declare_b.get("intent")))
    return (s_scope + s_intent) / 2.0


def similar(root, declare, k=None, tasks=None):
    """비슷한 과거 작업(실측된 것만) 상위 k개 [(score, record)]."""
    k = k or config.get("task.similar_top_k", root)
    floor = config.get("task.similar_min_score", root)
    rows = tasks if tasks is not None else ledger.read_tasks(root)
    scored = []
    for r in rows:
        act = r.get("actual") or {}
        if not act.get("measured"):
            continue
        sc = score(declare, r.get("declare") or {})
        if sc >= floor:
            scored.append((sc, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:k]


def _minutes(sec):
    if sec is None:
        return "?"
    return "%d분" % max(1, int(math.ceil(float(sec) / 60.0)))


def _ktok(n):
    if n is None:
        return "?"
    return "%dk" % int(round(float(n) / 1000.0))


def similar_text(items):
    if not items:
        return None
    lines = ["비슷한 과거 작업 실측(예측 참고):"]
    for _, r in items:
        d = r.get("declare") or {}
        act = r.get("actual") or {}
        pred = (d.get("predict") or {}).get("agent_sec") or {}
        lines.append("- %s — 에이전트 %s · 토큰 %s (당시 예측 P50 %s · P80 %s)" % (
            (d.get("intent") or "")[:80], _minutes(act.get("agent_sec")), _ktok(act.get("tokens")),
            _minutes(pred.get("p50")), _minutes(pred.get("p80"))))
    return "\n".join(lines)


def estimate_line(declare):
    """개발자에게 보일 한 줄 "예상 8~15분 · 끝: 커밋까지"(P50~P80과 어디까지 하는지 — 끝이 틀렸으면 사람이 바로잡는다, spec 13)."""
    sec = (declare.get("predict") or {}).get("agent_sec") or {}
    lo, hi = sec.get("p50"), sec.get("p80")
    if lo is None or hi is None:
        return None
    a = max(1, int(math.ceil(float(lo) / 60.0)))
    b = max(a, int(math.ceil(float(hi) / 60.0)))
    line = "예상 %d~%d분" % (a, b) if b != a else "예상 %d분" % a
    end = str(declare.get("end") or "").strip()
    return "%s · 끝: %s" % (line, end) if end else line


def calibration(tasks):
    """실측된 작업으로 보정 점수. {"n", "agent_sec": {"p30": {"hit","target","gap"}, ...,"over_p80"}, "tokens": {...}}"""
    out = {"n": 0}
    for metric in ("agent_sec", "tokens"):
        hits = {lv: 0 for lv, _ in LEVELS}
        n = over = 0
        ratios = []
        for r in tasks:
            act = r.get("actual") or {}
            if not act.get("measured") or act.get(metric) is None:
                continue
            pred = ((r.get("declare") or {}).get("predict") or {}).get(metric) or {}
            if any(pred.get(lv) is None for lv, _ in LEVELS):
                continue
            n += 1
            for lv, _ in LEVELS:
                if float(act[metric]) <= float(pred[lv]):
                    hits[lv] += 1
            if float(act[metric]) > float(pred["p80"]):
                over += 1
            if float(pred["p50"]) > 0:
                ratios.append(float(act[metric]) / float(pred["p50"]))
        m = {"n": n, "over_p80": over, "ratio_p50_median": round(_median(ratios), 3) if ratios else None}
        for lv, target in LEVELS:
            hit = hits[lv] / float(n) if n else None
            m[lv] = {"hit": round(hit, 3) if hit is not None else None, "target": target,
                     "gap": round(hit - target, 3) if hit is not None else None}
        out[metric] = m
        out["n"] = max(out["n"], n)
    return out


def _median(xs):
    xs = sorted(xs)
    k = len(xs)
    return xs[k // 2] if k % 2 else (xs[k // 2 - 1] + xs[k // 2]) / 2.0


def bias(root, tasks=None):
    """이 PC의 최근 실측 작업(task.bias_window건)으로 본 예측 치우침 — calibration()의 실측/P50 중앙값. 표본이 모자라면 None."""
    from vmc_harness.core.project import pc_id
    rows = tasks if tasks is not None else ledger.read_tasks(root)
    me = pc_id()
    done = [r for r in rows if (r.get("actual") or {}).get("measured") and r.get("pc_id", me) == me]
    done.sort(key=lambda r: str(r.get("ended_at") or r.get("started_at") or ""))
    recent = done[-int(config.get("task.bias_window", root)):]
    cal = calibration(recent)
    if cal["n"] < int(config.get("task.bias_min_n", root)):
        return None
    return cal


def bias_text(root, cal):
    """착수 때 넣는 한 줄: "이 PC 최근 N건: 시간 실측/P50 중앙값 0.28 — 크게 잡는 편 · 토큰 1.36 — 작게 잡는 편"(spec 13 predict)."""
    if not cal:
        return None
    margin = float(config.get("task.bias_margin", root))
    parts = []
    for metric, label in (("agent_sec", "시간"), ("tokens", "토큰")):
        m = cal.get(metric) or {}
        r = m.get("ratio_p50_median")
        if r is None:
            continue
        lean = "작게 잡는 편" if r >= margin else ("크게 잡는 편" if r <= 1.0 / margin else "대체로 맞음")
        parts.append("%s %.2f — %s" % (label, r, lean))
    if not parts:
        return None
    return "이 PC 최근 %d건 예측 치우침(실측/P50 중앙값): %s" % (cal["n"], " · ".join(parts))


def touch_summary(tasks):
    vals = [((r.get("touch") or {}).get("in_scope"), (r.get("touch") or {}).get("declared_used")) for r in tasks]
    ins = [a for a, _ in vals if a is not None]
    used = [b for _, b in vals if b is not None]
    return {"in_scope_avg": round(sum(ins) / len(ins), 3) if ins else None,
            "declared_used_avg": round(sum(used) / len(used), 3) if used else None,
            "out_of_scope_tasks": sum(1 for r in tasks if r.get("out_of_scope"))}


def overdesign_summary(tasks):
    fired = [r for r in tasks if (r.get("overdesign") or {}).get("signals")]
    trimmed = [r for r in fired if (r.get("overdesign") or {}).get("trimmed")]
    return {"fired": len(fired), "trimmed": len(trimmed),
            "trimmed_ratio": round(len(trimmed) / float(len(fired)), 3) if fired else None}


def write_metrics(root, date_str=None):
    """일일 집계 파일(metrics/<pc_id>/<date>.json)의 "task" 키만 갱신한다(다른 키는 보존)."""
    tasks = ledger.read_tasks(root)
    date_str = date_str or now().strftime("%Y-%m-%d")
    path = ledger.metrics_path(root, date_str)
    data = read_json(path, {}) or {}
    from vmc_harness import KERNEL_VERSION
    data.setdefault("pc_id", path.parent.name)
    data.setdefault("date", date_str)
    data["kernel_version"] = KERNEL_VERSION
    today = [r for r in tasks if str(r.get("ended_at") or "").startswith(date_str)]
    data["task"] = {
        "tasks_today": len(today),
        "calibration_all": calibration(tasks),
        "touch_all": touch_summary(tasks),
        "overdesign_all": overdesign_summary(tasks),
        "harness_overhead_today": {
            "sec": round(sum(float((r.get("harness_overhead") or {}).get("sec") or 0) for r in today), 2),
            "tokens": sum(int((r.get("harness_overhead") or {}).get("tokens") or 0) for r in today),
            "small_sec_max": max([float((r.get("harness_overhead") or {}).get("sec") or 0)
                                  for r in today if r.get("size") == "small"] or [0.0]),
        },
    }
    write_json(path, data)
    return data


# ---- 세션 종료: 실측 + 작업 기록(09 스키마) ----

def _transcript(root, task, transcript_path=None):
    if transcript_path:
        return transcript_path
    sess = None
    try:
        from vmc_harness.task import state as st
        sess = st.session_get(root, task.get("session_id")) or {}
    except Exception:
        sess = {}
    if sess.get("transcript_path"):
        return sess["transcript_path"]
    try:
        from vmc_harness import sessions
        hit = sessions.find_by_id(root, task.get("session_id"), task.get("agent"))
        return str(hit[1]) if hit else None
    except Exception:
        return None


def windows(task):
    """작업 구간 [(start, end|None)] aware datetime — 선언~마감(다시 열린 작업은 구간 여러 개)."""
    from vmc_harness.core.clock import parse_iso
    segs = task.get("segments") or [{"start": task.get("declared_at") or task.get("started_at"),
                                     "end": task.get("ended_at")}]
    out = []
    for sg in segs:
        s0 = parse_iso(sg.get("start"))
        if s0 is None:
            continue
        out.append((s0, parse_iso(sg.get("end"))))
    return out


def _in_windows(at, wins):
    from vmc_harness.core.clock import parse_iso
    d = parse_iso(at) if isinstance(at, str) else at
    if d is None:
        return False
    return any(d >= s and (e is None or d <= e) for s, e in wins)


def _hook_sec(root, task):
    """디스패처가 남긴 gate_time(전체 훅 체인, 이 세션·작업 구간 안) 합. 없으면 None(task 핸들러 자체 측정값을 쓴다)."""
    try:
        from vmc_harness.core import events
        rows = events.read(root, "gate_time")
    except Exception:
        return None
    wins = windows(task)
    # 디스패처 행만(생성 게이트 행은 그 안에 이미 들어 있다 — total_sec). 옛 행은 sec
    secs = [float(r.get("total_sec") if r.get("total_sec") is not None else (r.get("sec") or 0)) for r in rows
            if str(r.get("gate_id") or "").startswith("dispatcher:")
            and r.get("session_id") == task.get("session_id") and _in_windows(r.get("at"), wins)]
    return sum(secs) if secs else None


def actual_of(session):
    """세션 파싱 결과 → 장부 actual. tokens = 캐시 읽기 제외(입력 + 캐시 생성 + 출력) — 예측과 같은 단위(D2).
    tokens_detail에 input·output·cache_read·cache_creation을 나눠 둔다."""
    parts = dict(session.token_parts or {})
    return {"tokens": session.work_tokens(), "tokens_detail": parts, "agent_sec": session.agent_sec,
            "tool_calls": len(session.tool_calls), "human_turns": session.human_turns, "measured": True}


def finalize(root, task, transcript_path=None, agent=None):
    """무거운 측정(마감 뒤 — 백그라운드·밤 루프). 작업 구간만 잘라 세션 기록 실측 → 장부 기록 덮어쓰기 → 일일 집계.
    아직 안 끝난 작업이면 먼저 마감 표시(세션 종료)."""
    from vmc_harness.task import lifecycle, scope, state as st
    if not task.get("ended_at"):
        task = lifecycle.close(root, task, "session_end")
    task = st.by_id(root, task["task_id"]) or task
    agent = agent or task.get("agent") or "claude"
    tp = _transcript(root, task, transcript_path)
    actual = {"tokens": None, "tokens_detail": None, "agent_sec": None, "tool_calls": None, "human_turns": None,
              "measured": False}
    phase_v, phase_d = None, None
    if tp:
        try:
            from vmc_harness import sessions
            s = sessions.parse(tp, agent, windows=windows(task))
            if s.measured:
                actual = actual_of(s)
                try:  # 단계 나누기(spec 13) — 실패해도 실측은 남긴다
                    from vmc_harness.task import phases as _ph
                    phase_v, phase_d = _ph.compute(s)
                except Exception:
                    phase_v, phase_d = None, None
        except Exception:
            pass
    od = dict(task.get("overdesign") or {})
    if od.get("reviewed") and od.get("lines_before") is not None:
        try:
            files = (task.get("scope_cache") or {}).get("files")
            od["lines_after"] = scope.added_lines(root, task, files)
            od["trimmed"] = od["lines_after"] < od["lines_before"]
        except Exception:
            pass
    try:
        from vmc_harness.gates.evidence import list_markers
        task["evidence_list"] = ["%s:%s" % (m.get("kind"), m.get("target"))
                                 for m in list_markers(root, task["task_id"]) or []]
    except Exception:
        pass
    over = task.get("overhead") or {}
    hook_sec = _hook_sec(root, task)
    fields = {
        "measured_overhead_sec": round((hook_sec if hook_sec is not None else float(over.get("sec") or 0))
                                       + float(over.get("declare_sec") or 0), 3),
        "gate_sec": round(hook_sec, 3) if hook_sec is not None else None,
        "actual": actual, "measure_pending": False, "measured_at": now().isoformat(timespec="seconds"),
        "phases": phase_v, "phases_detail": phase_d, "analysis": None,
        "overdesign": dict(task.get("overdesign") or {}, **{k: od.get(k) for k in ("lines_after", "trimmed")}),
        "evidence_list": task.get("evidence_list") or [], "scope_cache": task.get("scope_cache"),
    }
    # 재는 동안 다시 열렸으면(같은 세션 활동) 버린다 — 다시 마감될 때 전체 구간으로 다시 잰다
    cur = st.by_id(root, task["task_id"]) or task
    if cur.get("ended_at") != task.get("ended_at"):
        return None
    cur.update(fields)
    task = cur
    st.save(root, task)
    rec = lifecycle.publish(root, task, "ended")
    try:
        write_metrics(root, str(task.get("ended_at") or "")[:10] or None)
    except Exception:
        pass
    return rec
