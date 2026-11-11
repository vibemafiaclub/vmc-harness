"""일일 집계 `ledger/metrics/<pc_id>/<date>.json` (spec 09) 과 팀 합산.

원본은 PC 로컬 events.jsonl — 집계는 거기서 결정적으로 다시 계산한다(같은 날짜를 다시 돌려도 같은 결과).
집계에는 숫자·id만 넣는다(원문 없음, P9). 판정(judge)은 여기서 `exposures`만 읽는다.

읽는 이벤트 kind(다른 영역이 core.events.log로 남기는 것):
- gate_fire / gate_block / proceed_anyway(헛막기) / block_disputed(에이전트 반박 — 참고 지표) / gate_time(sec)   — 필드 gate_id
- exposure                                                            — 필드 action_ids, card(카드별 반환 수), gate_id
- card_search                                                         — 필드 sec, tokens(오버헤드만)
- session_selfcheck                                                   — 필드 human_turns, tool_calls, corrections, measured
- harness_overhead                                                    — 필드 sec, tokens
- human_call / wait                                                   — 세션당 사람 개입 참고
- verify_run                                                          — 고객 검증 명령(테스트·빌드·린트·타입체크·git commit 훅 근사) 소요.
                                                                         필드 gate·kinds·sec·exit_code·approx(gates.verify_timing). 하네스 오버헤드가 아니다
로컬 `loop-runs.jsonl`(qa·loop 영역)은 날짜별 실행 수·토큰·중단 사유만 센다.
"""

from vmc_harness import KERNEL_VERSION
from vmc_harness.core import paths
from vmc_harness.core.clock import now, now_iso, parse_iso
from vmc_harness.core.jsonio import read_json, read_jsonl, write_json
from vmc_harness.core.project import local_dir, pc_id

GATE_KINDS = {"gate_fire": "fired", "gate_block": "blocked", "proceed_anyway": "false_block", "block_disputed": "disputed"}


def _date_of(at):
    d = parse_iso(at)
    return d.astimezone().strftime("%Y-%m-%d") if d else None


def _empty(date_str, pc):
    return {"pc_id": pc, "date": date_str, "kernel_version": KERNEL_VERSION,
            "gates": {}, "cards": {}, "exposures": {},
            "sessions": 0, "human_turns": 0, "human_turns_per_session": None,
            "tool_calls": 0, "corrections": 0, "unmeasured_sessions": 0,
            "human_calls": 0, "waits": 0,
            "overhead": {"sec": 0.0, "tokens": 0},
            "loop": {"runs": 0, "tokens": 0, "stop_reasons": {}},
            "verify": {},
            "updated_at": None}


def _gate(m, gid):
    return m["gates"].setdefault(gid, {"fired": 0, "blocked": 0, "false_block": 0, "disputed": 0, "sec": 0.0})


def _num(v, default=0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def aggregate(events, loop_runs=(), pc=None):
    """이벤트 목록 → {date: 집계}. 순수 함수(테스트용)."""
    pc = pc or pc_id()
    out = {}
    for e in events:
        d = _date_of(e.get("at"))
        if not d:
            continue
        m = out.setdefault(d, _empty(d, pc))
        k = e.get("kind")
        gid = e.get("gate_id")
        if k in GATE_KINDS and gid:
            _gate(m, gid)[GATE_KINDS[k]] += 1
        elif k == "gate_time" and gid:
            g = _gate(m, gid)
            g["sec"] = round(g["sec"] + _num(e.get("sec")), 3)
            m["overhead"]["sec"] = round(m["overhead"]["sec"] + _num(e.get("sec")), 3)
        elif k == "exposure":
            for aid in e.get("action_ids") or []:
                m["exposures"][aid] = m["exposures"].get(aid, 0) + 1
            if e.get("card"):  # 카드별 반환 수 = 카드 노출(검색 이벤트는 여러 곳에서 남아 중복될 수 있다)
                m["cards"][e["card"]] = m["cards"].get(e["card"], 0) + 1
        elif k == "card_search":
            m["overhead"]["sec"] = round(m["overhead"]["sec"] + _num(e.get("sec")), 3)
            m["overhead"]["tokens"] += int(_num(e.get("tokens")))
        elif k == "session_selfcheck":
            m["sessions"] += 1
            if e.get("measured"):
                m["human_turns"] += int(_num(e.get("human_turns")))
                m["tool_calls"] += int(_num(e.get("tool_calls")))
                m["corrections"] += int(_num(e.get("corrections")))
            else:
                m["unmeasured_sessions"] += 1
        elif k == "harness_overhead":
            m["overhead"]["sec"] = round(m["overhead"]["sec"] + _num(e.get("sec")), 3)
            m["overhead"]["tokens"] += int(_num(e.get("tokens")))
        elif k == "verify_run" and e.get("gate"):
            v = m["verify"].setdefault(e["gate"], {"runs": 0, "sec": 0.0, "fails": 0, "unknown_exit": 0,
                                                    "kinds": e.get("kinds") or [], "approx": bool(e.get("approx"))})
            v["runs"] += 1
            v["sec"] = round(v["sec"] + _num(e.get("sec")), 3)
            if e.get("exit_code") is None:
                v["unknown_exit"] += 1
            elif e.get("exit_code") != 0:
                v["fails"] += 1
        elif k == "human_call":
            m["human_calls"] += 1
        elif k == "wait":
            m["waits"] += 1
    for r in loop_runs:
        d = _date_of(r.get("at") or r.get("started_at"))
        if not d:
            continue
        m = out.setdefault(d, _empty(d, pc))
        m["loop"]["runs"] += 1
        m["loop"]["tokens"] += int(_num(r.get("tokens")))
        reason = r.get("stop_reason") or r.get("reason")
        if reason:
            m["loop"]["stop_reasons"][reason] = m["loop"]["stop_reasons"].get(reason, 0) + 1
    for m in out.values():
        measured = m["sessions"] - m["unmeasured_sessions"]
        m["human_turns_per_session"] = round(m["human_turns"] / measured, 2) if measured else None
    return out


def rollup(root, dates=None):
    """이 PC의 로컬 이벤트로 일일 집계 파일을 다시 쓴다. 이벤트가 없는 날짜 파일은 건드리지 않는다.
    dates가 없으면 이벤트가 있는 모든 날짜(로컬 보관 기간 안). 쓴 날짜 목록을 돌려준다."""
    ldir = local_dir(root)
    events = read_jsonl(ldir / "events.jsonl")
    loops = read_jsonl(ldir / "loop-runs.jsonl")
    pc = pc_id()
    agg = aggregate(events, loops, pc)
    written = []
    from vmc_harness.core import ledger
    for d, m in sorted(agg.items()):
        if dates and d not in dates:
            continue
        p = ledger.metrics_path(root, d, pc)
        old = read_json(p, None) or {}
        # 다른 영역이 같은 파일에 쓴 키(예: task 영역의 "task")는 보존한다 — 이벤트에서 다시 계산하는 키만 갈아 끼운다
        m = dict(old, **m)
        m["updated_at"] = old.get("updated_at")
        if {k: v for k, v in old.items() if k != "updated_at"} != {k: v for k, v in m.items() if k != "updated_at"}:
            m["updated_at"] = now_iso()
            write_json(p, m)
            written.append(d)
    return written


def load_all(root, since=None):
    """팀 전체(모든 PC) 일일 집계 목록. since = 'YYYY-MM-DD' 이상만."""
    base = paths.ledger_dir(root) / "metrics"
    out = []
    if not base.is_dir():
        return out
    for f in sorted(base.glob("*/*.json")):
        m = read_json(f, None)
        if not m:
            continue
        if since and (m.get("date") or f.stem) < since:
            continue
        out.append(m)
    return out


def exposures(root, action_id, since=None, rows=None):
    rows = load_all(root, since) if rows is None else rows
    return sum(int((m.get("exposures") or {}).get(action_id, 0)) for m in rows
               if not since or (m.get("date") or "") >= since)


def card_uses(root, card_id, since=None, rows=None):
    rows = load_all(root, since) if rows is None else rows
    return sum(int((m.get("cards") or {}).get(card_id, 0)) for m in rows
               if not since or (m.get("date") or "") >= since)


def gate_totals(root, gate_id, since=None, rows=None):
    rows = load_all(root, since) if rows is None else rows
    t = {"fired": 0, "blocked": 0, "false_block": 0, "disputed": 0, "sec": 0.0}
    for m in rows:
        if since and (m.get("date") or "") < since:
            continue
        g = (m.get("gates") or {}).get(gate_id)
        if g:
            for k in t:
                t[k] += g.get(k, 0)
    return t


def team(root, since=None):
    """팀 합산 + 참고 지표(판정에 쓰지 않는다 — 04 §참고 지표)."""
    rows = load_all(root, since)
    t = {"pcs": sorted({m.get("pc_id") for m in rows if m.get("pc_id")}), "days": len(rows),
         "sessions": 0, "human_turns": 0, "tool_calls": 0, "corrections": 0, "unmeasured_sessions": 0,
         "overhead": {"sec": 0.0, "tokens": 0}, "gates": {}, "cards": {}, "loop": {"runs": 0, "tokens": 0}}
    for m in rows:
        for k in ("sessions", "human_turns", "tool_calls", "corrections", "unmeasured_sessions"):
            t[k] += int(m.get(k) or 0)
        t["overhead"]["sec"] = round(t["overhead"]["sec"] + _num((m.get("overhead") or {}).get("sec")), 3)
        t["overhead"]["tokens"] += int(_num((m.get("overhead") or {}).get("tokens")))
        for gid, g in (m.get("gates") or {}).items():
            tg = t["gates"].setdefault(gid, {"fired": 0, "blocked": 0, "false_block": 0, "disputed": 0, "sec": 0.0})
            for k in tg:
                tg[k] += g.get(k, 0)
        for cid, n in (m.get("cards") or {}).items():
            t["cards"][cid] = t["cards"].get(cid, 0) + n
        lp = m.get("loop") or {}
        t["loop"]["runs"] += int(lp.get("runs") or 0)
        t["loop"]["tokens"] += int(lp.get("tokens") or 0)
    measured = t["sessions"] - t["unmeasured_sessions"]
    t["reference"] = {
        # 정정 유형 재발률은 도구 호출 수당 밀도로 본다(과제가 커져 늘어난 개입을 악화로 읽지 않게)
        "corrections_per_100_tool_calls": round(100.0 * t["corrections"] / t["tool_calls"], 2) if t["tool_calls"] else None,
        "human_turns_per_session": round(t["human_turns"] / measured, 2) if measured else None,
        "overhead_sec_per_session": round(t["overhead"]["sec"] / t["sessions"], 2) if t["sessions"] else None,
        "calibration": calibration(root, since),
        "verification_wait": verification_wait(root, since, rows),
    }
    return t


def verification_wait(root, since=None, rows=None, until=None):
    """검증 게이트별 평균 소요 × 횟수 = 주당 대기 시간 + 재배치 후보(참고 사실 — finding·판정 입력이 아니다, 04).

    주당 횟수 = 기간 실행 수 × 7 / max(기간 일수, 7) — 일주일이 안 된 데이터는 늘려 잡지 않는다.
    후보 = 1회 평균 ≥ gates.verify_slow_sec 이고 주당 ≥ gates.verify_frequent_per_week. 하네스는 옮기지 않고 제안 문구만 둔다."""
    from vmc_harness.core import config
    rows = load_all(root, since) if rows is None else rows
    agg, dates = {}, []
    for m in rows:
        d = m.get("date") or ""
        if (since and d < since) or (until and d > until):
            continue
        for gate, v in (m.get("verify") or {}).items():
            if not v.get("runs"):
                continue
            dates.append(d)
            a = agg.setdefault(gate, {"runs": 0, "sec": 0.0, "fails": 0, "unknown_exit": 0,
                                      "kinds": v.get("kinds") or [], "approx": bool(v.get("approx"))})
            for key in ("runs", "fails", "unknown_exit"):
                a[key] += int(v.get(key) or 0)
            a["sec"] += _num(v.get("sec"))
    if not agg:
        return {"days": 0, "gates": [], "candidates": [], "total_wait_sec_per_week": 0.0}
    import datetime as _dt
    first = since or min(dates)
    last = until or max(dates)
    try:
        span = (_dt.date.fromisoformat(last) - _dt.date.fromisoformat(first)).days + 1
    except ValueError:
        span = len(set(dates))
    week_days = 7
    weeks = max(span, week_days) / float(week_days)
    slow = float(config.get("gates.verify_slow_sec", root))
    freq = float(config.get("gates.verify_frequent_per_week", root))
    gates_, cands = [], []
    for gate, a in agg.items():
        avg = a["sec"] / a["runs"]
        per_week = a["runs"] / weeks
        g = {"gate": gate, "kinds": a["kinds"], "approx": a["approx"], "runs": a["runs"], "avg_sec": round(avg, 1),
             "runs_per_week": round(per_week, 1), "wait_sec_per_week": round(avg * per_week, 1),
             "fails": a["fails"], "unknown_exit": a["unknown_exit"]}
        gates_.append(g)
        if avg >= slow and per_week >= freq:
            c = dict(g)
            c["suggestion"] = ("'%s' 1회 평균 %d초 × 주 %d회 = 주 %d분 대기%s — 덜 자주 도는 자리(배포 전·push 전 등)로 옮길 후보. "
                               "하네스는 옮기지 않는다" % (gate, round(avg), round(per_week), round(avg * per_week / 60),
                                                          " (커밋 전체 소요로 근사)" if a["approx"] else ""))
            cands.append(c)
    gates_.sort(key=lambda g: -g["wait_sec_per_week"])
    cands.sort(key=lambda g: -g["wait_sec_per_week"])
    return {"days": span, "gates": gates_, "candidates": cands,
            "total_wait_sec_per_week": round(sum(g["wait_sec_per_week"] for g in gates_), 1)}


def calibration(root, since=None):
    """예측 보정: P30/P50/P80 안에 끝난 비율(측정된 작업만). 참고 지표."""
    from vmc_harness.core import ledger
    hit = {"p30": 0, "p50": 0, "p80": 0}
    n = 0
    for t in ledger.read_tasks(root):
        if since and (t.get("started_at") or "")[:10] < since:
            continue
        act = t.get("actual") or {}
        pred = ((t.get("declare") or {}).get("predict") or {}).get("agent_sec") or {}
        if not act.get("measured") or not pred.get("p80"):
            continue
        n += 1
        for k in hit:
            if pred.get(k) and _num(act.get("agent_sec")) <= _num(pred.get(k)):
                hit[k] += 1
    if not n:
        return None
    return {"tasks": n, "p30": round(hit["p30"] / n, 2), "p50": round(hit["p50"] / n, 2), "p80": round(hit["p80"] / n, 2)}


def today():
    return now().strftime("%Y-%m-%d")
