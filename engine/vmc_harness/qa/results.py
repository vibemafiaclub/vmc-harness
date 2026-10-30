"""QA 결과 원본 `.harness/qa/results.json` (spec 05·09) — 이 파일 하나가 원본이다(HTML은 여기서 만들기만).

상태 갱신은 이 모듈 함수(= `vmc-harness qa status set`)로만 한다. 시간·토큰은 **코드가 센다**:
- 시계는 `qa run`(begin)에서 시작해, FAIL 뒤 수정하는 동안에도 같은 세션이면 계속 센다(수정 시간 포함)
- 토큰은 지금 세션 기록의 토큰 합 차이(sessions 영역 파서). 읽지 못하면 미측정(시간만으로 판단)
- 시나리오별 누적이 `qa.scenario_budget_sec`·`qa.scenario_budget_tokens`를 넘으면 HOLD(사람 목록으로)
예산 숫자는 에이전트에게 보이지 않는다(01 P5) — 넘으면 조용히 HOLD로 바꾸기만 한다.

시나리오 기록 형식:
{"status": "PASS|FAIL|HOLD", "cause": "모델이 쓴 원인 설명(분류 고정 안 함)",
 "needs_human": bool, "needs_db_change": bool,        # 필수 칸 둘
 "evidence": ["<증거 경로>"], "ever_passed": bool, "last_pass_at": "...",
 "budget": {"sec": 0.0, "tokens": 0, "tokens_measured": true, "exceeded": false},
 "clock": {"since": "...", "session": "<세션 파일>", "tokens_at": 0} | null,
 "attempts": [{"started_at","ended_at","status","cause","sec","tokens","evidence","by"}],
 "updated_at": "...", "by": "agent|human"}
"""

from pathlib import Path

from vmc_harness import KERNEL_VERSION
from vmc_harness.core import config, paths
from vmc_harness.core.clock import now, now_iso, parse_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.ledger import MetaGateError
from vmc_harness.core.mask import mask

STATUSES = ("PASS", "FAIL", "HOLD")


def results_path(root):
    return paths.harness_dir(root) / "qa" / "results.json"


def load(root):
    data = read_json(results_path(root), None)
    if not isinstance(data, dict):
        data = {"version": 1, "scenarios": {}}
    data.setdefault("scenarios", {})
    return data


def _save(root, data):
    data["updated_at"] = now_iso()
    write_json(results_path(root), data)


def get(root, sid):
    return load(root)["scenarios"].get(sid)


def _empty():
    return {"status": None, "cause": None, "needs_human": None, "needs_db_change": None,
            "evidence": [], "ever_passed": False, "last_pass_at": None,
            "budget": {"sec": 0.0, "tokens": 0, "tokens_measured": True, "exceeded": False},
            "clock": None, "attempts": [], "updated_at": None, "by": None}


# ---- 세션 토큰(코드가 센다) ----

def _current_session(root):
    """(세션 파일 경로, 토큰 합) — 이 프로젝트의 가장 최근 세션. 못 읽으면 (None, None)."""
    try:
        from vmc_harness import sessions
        found = None
        try:
            from vmc_harness.task import state
            found = sessions.find_by_id(str(root), state.resolve_session_id(root))
        except (ImportError, AttributeError):
            pass
        if not found:
            lst = sessions.find(str(root), limit=1)
            found = lst[0] if lst else None
        if not found:
            return None, None
        agent, path = found
        s = sessions.parse(path, agent)
        if not getattr(s, "measured", False):
            return str(path), None
        return str(path), int(getattr(s, "tokens", 0) or 0)
    except Exception:
        return None, None


def _tick(rec, root):
    """시계가 돌고 있으면 경과를 예산에 더하고 시계를 지금으로 옮긴다. (sec, tokens|None) 반환."""
    clock = rec.get("clock")
    sess, tok = _current_session(root)
    sec, tokens = 0.0, None
    if clock:
        since = parse_iso(clock.get("since"))
        same = (clock.get("session") or None) == (sess or None)
        if since and same:
            sec = max(0.0, (now() - since).total_seconds())
        if same and tok is not None and clock.get("tokens_at") is not None:
            tokens = max(0, tok - int(clock["tokens_at"]))
    b = rec["budget"]
    b["sec"] = round(float(b.get("sec") or 0) + sec, 1)
    if tokens is None:
        if clock:
            b["tokens_measured"] = False
    else:
        b["tokens"] = int(b.get("tokens") or 0) + tokens
    rec["clock"] = {"since": now_iso(), "session": sess, "tokens_at": tok}
    return sec, tokens


def _over_budget(rec, root):
    b = rec["budget"]
    if b.get("sec", 0) > config.get("qa.scenario_budget_sec", root):
        return True
    if b.get("tokens", 0) > config.get("qa.scenario_budget_tokens", root):
        return True
    return False


# ---- 갱신 ----

def begin(root, sid):
    """`qa run` — 시도 시작. 예산을 이미 넘었으면 HOLD로 두고 돌려준다."""
    data = load(root)
    rec = data["scenarios"].setdefault(sid, _empty())
    if rec.get("status") == "HOLD" and rec["budget"].get("exceeded"):
        return rec
    if rec.get("status") == "PASS":  # 통과 뒤 다시 돌리는 것은 새 회차 — 예산을 새로 센다
        rec["budget"] = {"sec": 0.0, "tokens": 0, "tokens_measured": True, "exceeded": False}
        rec["clock"] = None
    _tick(rec, root)
    if _over_budget(rec, root):
        _hold_for_budget(rec)
    else:
        rec["running_since"] = rec["clock"]["since"]
    rec["updated_at"] = now_iso()
    _save(root, data)
    return rec


def _hold_for_budget(rec):
    rec["budget"]["exceeded"] = True
    rec["status"] = "HOLD"
    rec["clock"] = None
    rec.pop("running_since", None)
    if not rec.get("cause"):
        rec["cause"] = "시나리오 시간·토큰 예산 초과"
    rec["attempts"].append({"started_at": None, "ended_at": now_iso(), "status": "HOLD",
                            "cause": "budget", "sec": 0, "tokens": 0, "evidence": [], "by": "harness"})


def _norm_evidence(root, evidence):
    out = []
    for e in evidence or []:
        p = Path(e)
        if not p.is_absolute():
            p = Path(root) / e
        if not p.exists():
            raise MetaGateError("증거 파일 없음: %s" % e)
        try:
            out.append(p.resolve().relative_to(Path(root).resolve()).as_posix())
        except ValueError:
            out.append(p.as_posix())
    return out


def set_status(root, sid, status, cause=None, needs_human=None, needs_db_change=None,
               evidence=None, by="agent", reset_budget=False):
    """상태 갱신(코드 경유 유일 경로). 필수 칸: FAIL·HOLD는 needs_human·needs_db_change(bool) + 원인,
    에이전트의 PASS는 증거 1개 이상. 에이전트는 `qa run`으로 시작한 시나리오만 갱신한다."""
    status = (status or "").upper()
    if status not in STATUSES:
        raise MetaGateError("상태 오류: %s (PASS|FAIL|HOLD)" % status)
    if status in ("FAIL", "HOLD"):
        if not isinstance(needs_human, bool):
            raise MetaGateError("필수 칸 없음: 사람 필요 여부")
        if not isinstance(needs_db_change, bool):
            raise MetaGateError("필수 칸 없음: DB 변경 필요 여부")
        if not str(cause or "").strip():
            raise MetaGateError("원인 설명 없음")
    ev = _norm_evidence(root, evidence)
    data = load(root)
    rec = data["scenarios"].setdefault(sid, _empty())
    if by == "agent":
        if status == "PASS" and not ev:
            raise MetaGateError("PASS 증거 없음")
        if not rec.get("running_since"):
            raise MetaGateError("qa run으로 시작하지 않은 시나리오: %s" % sid)
        if rec["budget"].get("exceeded"):
            raise MetaGateError("예산 초과로 HOLD된 시나리오: %s" % sid)
    if by == "human" and reset_budget:
        rec["budget"] = {"sec": 0.0, "tokens": 0, "tokens_measured": True, "exceeded": False}
        rec["clock"] = None
    started = rec.get("running_since")
    sec, tokens = (0.0, None)
    if rec.get("clock"):
        sec, tokens = _tick(rec, root)
    attempt = {"started_at": started, "ended_at": now_iso(), "status": status,
               "cause": mask(cause) if cause else None, "sec": round(sec, 1), "tokens": tokens,
               "evidence": ev, "by": by, "kernel_version": KERNEL_VERSION}
    rec["attempts"].append(attempt)
    rec["status"] = status
    rec["cause"] = mask(cause) if cause else (None if status == "PASS" else rec.get("cause"))
    rec["needs_human"] = bool(needs_human) if needs_human is not None else (False if status == "PASS" else rec.get("needs_human"))
    rec["needs_db_change"] = bool(needs_db_change) if needs_db_change is not None else (False if status == "PASS" else rec.get("needs_db_change"))
    if ev:
        rec["evidence"] = ev
    rec["by"] = by
    rec.pop("running_since", None)
    if status == "PASS":
        rec["ever_passed"] = True
        rec["last_pass_at"] = now_iso()
        rec["clock"] = None
    elif status == "HOLD":
        rec["clock"] = None
    else:  # FAIL — 같은 세션에서 고치는 시간도 센다
        if by == "agent" and _over_budget(rec, root):
            _hold_for_budget(rec)
    rec["updated_at"] = now_iso()
    _save(root, data)
    return rec


# ---- 조회 ----

def summary(root, scenario_ids=None):
    """{"total","PASS","FAIL","HOLD","PENDING"} — 시나리오 폴더 기준(결과 없는 것은 PENDING)."""
    from vmc_harness.qa import scenarios
    ids = scenario_ids
    if ids is None:
        ids = [sc.get("id") for sc, ok, _ in scenarios.load_all(root)]
    res = load(root)["scenarios"]
    out = {"total": len(ids), "PASS": 0, "FAIL": 0, "HOLD": 0, "PENDING": 0}
    for sid in ids:
        st = (res.get(sid) or {}).get("status") or "PENDING"
        out[st] = out.get(st, 0) + 1
    return out


def human_list(root):
    """사람에게 한 번에 보일 목록: 사람 필요·DB 변경 필요·예산 초과 HOLD (PASS 제외)."""
    out = []
    for sid, r in sorted(load(root)["scenarios"].items()):
        if r.get("status") == "PASS":
            continue
        reasons = []
        if r.get("needs_human"):
            reasons.append("사람 결정")
        if r.get("needs_db_change"):
            reasons.append("DB 변경")
        if (r.get("budget") or {}).get("exceeded"):
            reasons.append("예산 초과")
        if reasons:
            out.append({"id": sid, "status": r.get("status"), "reasons": reasons, "cause": r.get("cause"),
                        "evidence": r.get("evidence") or []})
    return out


def regressions(root, scenario_ids=None):
    """QA 회귀 — 통과한 적 있는 시나리오가 지금 FAIL·HOLD. finding이 아니라 머지를 막는 게이트 신호(01 P2)."""
    out = []
    for sid, r in sorted(load(root)["scenarios"].items()):
        if scenario_ids is not None and sid not in scenario_ids:
            continue
        if r.get("ever_passed") and r.get("status") in ("FAIL", "HOLD"):
            out.append(sid)
    return out


def all_pass(root, scenario_ids):
    res = load(root)["scenarios"]
    return bool(scenario_ids) and all((res.get(s) or {}).get("status") == "PASS" for s in scenario_ids)


def resume(root):
    """새 세션 "이어서" — 남은 것·다음 할 것. {"summary","next","remaining","human","invalid"}"""
    from vmc_harness.qa import scenarios
    loaded = scenarios.load_all(root)
    res = load(root)["scenarios"]
    invalid = [{"id": sc.get("id"), "reason": reason} for sc, ok, reason in loaded if not ok]
    valid_ids = [sc["id"] for sc, ok, _ in loaded if ok]
    human_ids = {h["id"] for h in human_list(root)}
    remaining = []
    for sid in valid_ids:
        r = res.get(sid) or {}
        st = r.get("status") or "PENDING"
        if st == "PASS" or sid in human_ids:
            continue
        if st == "HOLD":
            continue
        remaining.append({"id": sid, "status": st, "cause": r.get("cause"),
                          "running": bool(r.get("running_since"))})
    # 다음 할 것: 진행 중이던 것 → FAIL(고쳐서 재시험) → 아직 안 돌린 것
    remaining.sort(key=lambda x: (0 if x["running"] else (1 if x["status"] == "FAIL" else 2), x["id"]))
    return {"summary": summary(root, valid_ids), "next": remaining[0] if remaining else None,
            "remaining": remaining, "human": human_list(root), "invalid": invalid}
