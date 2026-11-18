"""판정 스크립트 (spec 04 §판정, 01 P7 — 모델이 아니라 숫자로).

입력은 오탐 0인 것만: 노출(일일 집계 exposures, 팀 전체 PC 합산) + 명시적 부정 finding.
- 시험 기간 = 노출 judge.exposures회, 최대 judge.max_days일
- revert·원인 지목·거절·삭제/제외 버튼 1회 → 즉시 원복
- 헛막기는 judge.false_block_immediate회 초과 시 즉시 원복, 노출을 다 채운 시점엔 1회라도 있으면 원복
- 노출을 채우고 부정 0 → 존속(kept). 단 시험 시작 뒤 judge.min_days일이 지나야 하고, 노출이 judge.min_hits 이상이어야 한다
  (노출 = 조건이 해당한 횟수 — 게이트는 gates/generated.py가 조건 해당 때만 남긴다)
- 존속·원복은 게이트 파일(.harness/gates/<id>.json)의 status·enabled에도 반영한다(revert.set_gate_status)
- 업데이트 뒤 첫 판정에서 근거 없이 난 옛 존속(게이트가 실제로 막은 적 min_hits 미만)을 시험 중으로 되돌린다(migrate)
- 기간이 끝났는데 노출 부족 → 원복. 단 사람이 요청한 조치(human_requested)는 노출 부족으로 원복하지 않는다 → extended
- 진단 자동 반영(basis diagnosis_auto)은 부작용·효과 지표를 더 본다(side_effects — 코드로 센 숫자만, LLM 해석 없음):
  그 게이트의 (헛막기 + 에이전트 반박) / 막은 횟수 ≥ judge.auto_side_bad_ratio(막은 횟수 judge.auto_side_min_blocks 이상일 때),
  반영 뒤 다시 잰 테스트 기준선에서 바꾼 모듈의 새 실패 > judge.auto_new_failures_max,
  반영 근거 신호(대화당 횟수)가 반영 뒤 대화 judge.auto_signal_min_sessions개 이상에서 judge.auto_signal_min_drop만큼 줄지 않음 → 원복.
  신호가 그만큼 줄었고 judge.min_days가 지났으면 노출 수와 무관하게 존속(가설 효과를 잰 것)
참고 지표(재발률·개입·보정·오버헤드)는 판정에 쓰지 않는다 — metrics.team()에만.
"""

import datetime as _dt

from vmc_harness.core import config, paths
from vmc_harness.core import ledger as core_ledger
from vmc_harness.core.clock import now, now_iso, parse_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.project import local_dir
from vmc_harness.ledger import metrics

OPEN = ("trial", "extended")
IMMEDIATE_KINDS = ("revert", "blame", "reject")
NEGATIVE_BUTTONS = ("delete", "exclude")


def _same_commit(a, b):
    a, b = (a or "").strip(), (b or "").strip()
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def finding_hits(action, f):
    """이 finding이 이 조치를 겨누나."""
    t = f.get("target") or {}
    at = action.get("target") or {}
    if action["action_id"] in ([t.get("action_id")] + list(t.get("action_ids") or [])):
        return True
    for k in ("gate_id", "card", "rule_id"):
        if t.get(k) and at.get(k) == t.get(k):
            return True
    if t.get("commit"):
        for c in (action.get("commits") or []):
            if _same_commit(c, t["commit"]):
                return True
    return False


def negatives(action, findings):
    """(즉시 원복 부정 수, 헛막기 수) — 조치 생성 이후 finding만."""
    created = parse_iso(action.get("created_at"))
    hard, false_blocks = 0, 0
    for f in findings:
        if not f or not finding_hits(action, f):
            continue
        at = parse_iso(f.get("at"))
        if created and at and at < created:
            continue
        k = f.get("kind")
        if k == "false_block":
            false_blocks += 1
        elif k in IMMEDIATE_KINDS or (k == "button" and f.get("button") in NEGATIVE_BUTTONS):
            hard += 1
    return hard, false_blocks


CONFLICT_KIND = "guide_conflict"


def conflict_count(root, action):
    """사람이 '아님'을 누르지 않은 지침 충돌 감지 수(서로 다른 대화) — 조치 생성 이후, 카드·규칙 대상만.
    출처: 원복 후보 알림(notices guide_conflict, 지침마다 한 번) + 감지 기록(events guide_conflict_seen, 감지마다).
    '아님'(events guide_conflict_dismissed)이 있으면 그 전 감지는 세지 않는다."""
    t = action.get("target") or {}
    gid = t.get("card") or t.get("rule_id")
    if not gid:
        return 0
    created = parse_iso(action.get("created_at"))
    seen = []
    notes, evs = _conflict_rows(root)
    for n in notes:
        r = n.get("refs") or {}
        if r.get("guide_id") == gid:
            seen.append((parse_iso(n.get("at")), r.get("session_id") or n.get("id")))
    cut = None
    for e in evs:
        k = e.get("kind")
        if k == "guide_conflict_seen" and e.get("guide_id") == gid:
            seen.append((parse_iso(e.get("at")), e.get("session_id") or e.get("at")))
        elif k == "guide_conflict_dismissed" and e.get("guide_id") == gid:
            at = parse_iso(e.get("at"))
            cut = at if cut is None or (at and at > cut) else cut
    sids = {sid for at, sid in seen if at and (not created or at >= created) and (not cut or at > cut)}
    return len(sids)


_CONFLICT_CACHE = {}


def _conflict_rows(root):
    """(충돌 알림, 충돌 관련 이벤트) — 파일 크기·수정 시각이 같으면 다시 읽지 않는다(판정 한 번에 조치 수만큼 부른다)."""
    from vmc_harness.core.jsonio import read_jsonl
    d = local_dir(root)
    key = []
    for name in ("notices.jsonl", "events.jsonl"):
        try:
            st = (d / name).stat()
            key.append((st.st_size, st.st_mtime))
        except OSError:
            key.append(None)
    hit = _CONFLICT_CACHE.get(str(d))
    if hit and hit[0] == key:
        return hit[1]
    notes = [n for n in read_jsonl(d / "notices.jsonl") if n.get("kind") == CONFLICT_KIND]
    evs = [e for e in read_jsonl(d / "events.jsonl")
           if e.get("kind") in ("guide_conflict_seen", "guide_conflict_dismissed")]
    _CONFLICT_CACHE[str(d)] = (key, (notes, evs))
    return notes, evs


def trial_start(action):
    """시험 시작 시각 — 다시 연 시험(reopened)이면 그 시각."""
    t = action.get("trial") or {}
    return parse_iso(t.get("started_at")) or parse_iso(action.get("created_at"))


def count_since(action):
    """노출을 세기 시작하는 날짜(YYYY-MM-DD). 다시 연 시험은 trial.count_from(옛 노출 정의로 센 날을 빼려고 다음 날부터)."""
    t = action.get("trial") or {}
    return t.get("count_from") or (action.get("created_at") or "")[:10] or None


def deadline_of(action, root=None):
    d = parse_iso((action.get("trial") or {}).get("deadline"))
    if d:
        return d
    c = parse_iso(action.get("created_at")) or now()
    return c + _dt.timedelta(days=config.get("judge.max_days", root))


def _evaluate_predict(root, action, findings, at):
    """예측 빗나감 조치(spec 13): 노출 = 머지 뒤 그 영역에서 분석된 작업, 그 원인 단계의 초과 중앙값이 조치 전보다
    작으면 존속, 아니면 원복. 기간이 끝났는데 노출이 모자라면 구조 조치는 판정을 미루고(extended) 나머지는 원복."""
    from vmc_harness.task.analysis import _median
    need = config.get("judge.exposures", root)
    pr = action.get("predict") or {}
    start = parse_iso(action.get("merged_at")) or trial_start(action) or at
    vals = []
    for t in core_ledger.read_tasks(root):
        an = t.get("analysis") or {}
        ex = (an.get("excess") or {}).get(pr.get("phase"))
        if ex is None or (parse_iso(t.get("ended_at")) or start) <= start:
            continue
        if pr.get("area") not in ("-", None) and pr.get("area") not in (an.get("area") or []):
            continue
        vals.append(float(ex))
    hard, fb = negatives(action, findings)
    base = pr.get("baseline_excess")
    med = _median(vals[-need:]) if vals else None
    nums = {"exposures": len(vals), "negatives": hard + fb, "false_blocks": fb, "needed": need,
            "excess_before": base, "excess_after": med}
    if hard:
        return "reverted", "명시적 부정 %d건" % hard, nums
    if len(vals) >= need and base is not None:
        if med < float(base):
            return "kept", "그 영역 %s 초과 중앙값 %.0f초 → %.0f초" % (pr.get("phase"), float(base), med), nums
        return "reverted", "그 영역 %s 초과가 줄지 않음(%.0f초 → %.0f초)" % (pr.get("phase"), float(base), med), nums
    if at >= deadline_of(action, root):
        if action.get("layer") == "structure":
            return "extended", "구조 조치 — 노출 부족(%d/%d)으로 판정을 미룸" % (len(vals), need), nums
        return "reverted", "기간 종료, 노출 부족(%d/%d) — 입증되지 않음" % (len(vals), need), nums
    return None, "시험 중", nums


def side_effects(root, action, rows, at=None):
    """진단 자동 반영 조치의 부작용·효과(코드로 센 숫자만). (원복 이유|None, 효과 이유|None, 숫자)."""
    since = count_since(action)
    side = {}
    gid = (action.get("target") or {}).get("gate_id")
    if gid:
        g = metrics.gate_totals(root, gid, since, rows)
        bad = g["false_block"] + g["disputed"]
        side.update(blocked=g["blocked"], disputed=g["disputed"], proceeded=g["false_block"])
        if g["blocked"] >= config.get("judge.auto_side_min_blocks", root) and g["blocked"] and \
                bad / float(g["blocked"]) >= config.get("judge.auto_side_bad_ratio", root):
            return "자동 반영 게이트의 헛막기·반박 %d/%d회" % (bad, g["blocked"]), None, side
    try:
        from vmc_harness.bootstrap import autoapply
    except ImportError:
        return None, None, side
    nf = autoapply.new_test_failures(root, action)
    if nf is not None:
        side["new_failures"] = len(nf)
        if len(nf) > config.get("judge.auto_new_failures_max", root):
            return "반영 뒤 테스트 새 실패 %d개(%s)" % (len(nf), ", ".join(nf[:3])), None, side
    sg = autoapply.signal_after(root, action)
    if sg:
        side["signal"] = sg
        if sg.get("undeterminable"):
            # 반영 직전에 이미 신호가 없었다 — 줄었는지·안 줄었는지 이 조치로 잴 수 없다. 존속·원복 근거로 쓰지 않는다(기간 규칙대로)
            return None, None, side
        if sg["sessions"] >= config.get("judge.auto_signal_min_sessions", root) and sg["after"] is not None:
            target = sg["before"] * (1 - config.get("judge.auto_signal_min_drop", root))
            if sg["after"] > target:
                return "근거 신호가 줄지 않음(대화당 %.2f → %.2f, 대화 %d개)" % (sg["before"], sg["after"], sg["sessions"]), None, side
            return None, "근거 신호 대화당 %.2f → %.2f(대화 %d개)" % (sg["before"], sg["after"], sg["sessions"]), side
    return None, None, side


def evaluate(root, action, findings, rows, at=None):
    """판정 1건. 반환 (result|None, reason, numbers). result: kept|reverted|extended|None(계속 시험)."""
    at = at or now()
    if (action.get("basis") or {}).get("type") == "predict_miss":
        return _evaluate_predict(root, action, findings, at)
    need = config.get("judge.exposures", root)
    fb_limit = config.get("judge.false_block_immediate", root)
    since = count_since(action)
    exp = metrics.exposures(root, action["action_id"], since, rows)
    hard, fb = negatives(action, findings)
    nums = {"exposures": exp, "negatives": hard + fb, "false_blocks": fb, "needed": need}
    if hard:
        return "reverted", "명시적 부정 %d건(revert·원인 지목·거절·삭제)" % hard, nums
    if fb > fb_limit:
        return "reverted", "헛막기 %d회(즉시 원복 기준 초과)" % fb, nums
    cc = conflict_count(root, action)
    if cc:
        nums["conflicts"] = cc
        if cc >= config.get("judge.conflict_revert", root):
            return "reverted", "사람 지시와 부딪힌 감지 %d회('아님' 없음)" % cc, nums
    if (action.get("basis") or {}).get("type") == "diagnosis_auto":
        bad, good, side = side_effects(root, action, rows, at)
        nums["side"] = side
        if bad:
            return "reverted", bad, nums
        days = (at - (trial_start(action) or at)).total_seconds() / 86400.0
        if good and not fb and days >= config.get("judge.min_days", root):
            return "kept", good, nums
    # 근거 신호를 재는 자동 반영 조치는 효과(신호 감소)로만 존속한다 — 규칙은 대화마다 읽혀 노출이 저절로 차므로
    # 노출 수는 존속 근거가 아니다. 효과를 잴 수 없으면(반영 직전 이미 신호 없음) 기간이 끝날 때 입증 안 됨으로 원복
    effect_only = bool((nums.get("side") or {}).get("signal"))
    if exp >= need and not effect_only:
        if fb:
            return "reverted", "노출 %d회 중 헛막기 %d회" % (exp, fb), nums
        min_hits = config.get("judge.min_hits", root)
        start = trial_start(action) or at
        days = (at - start).total_seconds() / 86400.0
        if exp >= min_hits and days >= config.get("judge.min_days", root):
            return "kept", "노출 %d회·%.1f일 동안 명시적 부정 0" % (exp, days), nums
    if at >= deadline_of(action, root):
        if action.get("human_requested"):
            return "extended", "사람 요청 조치 — 노출 부족(%d/%d)으로 원복하지 않음" % (exp, need), nums
        if effect_only:
            sg = nums["side"]["signal"]
            why = "반영 직전 이미 신호 없음 — 효과를 잴 수 없음" if sg.get("undeterminable") else "근거 신호 감소가 확인되지 않음"
            return "reverted", "기간 종료, %s — 입증되지 않음" % why, nums
        return "reverted", "기간 종료, 노출 부족(%d/%d) — 입증되지 않음" % (exp, need), nums
    return None, "시험 중", nums


def _side(root, action, rows, since):
    t = action.get("target") or {}
    side = {}
    if t.get("gate_id"):
        g = metrics.gate_totals(root, t["gate_id"], since, rows)
        side = {"overhead_sec": round(g["sec"], 3), "fired": g["fired"], "blocked": g["blocked"]}
    return side


def judge_actions(root, at=None):
    at = at or now()
    rows = metrics.load_all(root)
    findings = core_ledger.list_findings(root)
    out = []
    for a in core_ledger.list_actions(root):
        if not a or a.get("status") not in OPEN:
            continue
        result, reason, nums = evaluate(root, a, findings, rows, at)
        if result is None:
            continue
        since = count_since(a)
        verdict = {"action_id": a["action_id"], "period": [a.get("created_at"), at.isoformat(timespec="seconds")],
                   "exposures": nums["exposures"], "negatives": nums["negatives"],
                   "false_blocks": nums["false_blocks"], "side": dict(_side(root, a, rows, since), **(nums.get("side") or {})),
                   "result": result, "reason": reason, "judged_at": now_iso()}
        trial = dict(a.get("trial") or {})
        trial.update({"exposures": nums["exposures"], "negatives": nums["negatives"]})
        if result == "reverted":
            from vmc_harness.ledger import revert
            rec = revert.revert_action(root, a["action_id"], reason, by_human=False)
            if rec.get("status") != "reverted":
                verdict["deferred"] = rec.get("deferred")
                if (a.get("basis") or {}).get("type") == "predict_miss" and a.get("layer") == "structure":
                    # 위에 커밋이 쌓여 되돌리지 못한 구조 조치 — 원복을 새 구조 작업(티켓)으로 연다(spec 13)
                    c = core_ledger.create_candidate(root, "predict", "원복을 구조 작업으로: %s" % (a.get("hypothesis") or "")[:80])
                    core_ledger.update_candidate(root, c["candidate_id"], revert_of=a["action_id"],
                                                 predict_key="revert|%s" % a["action_id"],
                                                 cause=(a.get("predict") or {}).get("cause"),
                                                 area=(a.get("predict") or {}).get("area"),
                                                 targets=list(a.get("files") or [])[:20])
            core_ledger.update_action(root, a["action_id"], trial=trial)
        elif result == "kept":
            core_ledger.update_action(root, a["action_id"], status="kept", trial=trial)
            _sync_gate(root, a, "kept")
        else:  # extended — 한 기간 더
            trial["deadline"] = (deadline_of(a, root) + _dt.timedelta(days=config.get("judge.max_days", root))).isoformat(timespec="seconds")
            core_ledger.update_action(root, a["action_id"], status="extended", trial=trial)
        core_ledger.write_verdict(root, verdict)
        out.append(verdict)
    return out


def progress(root):
    """시험 중 조치 진행(쓰기 없음) — 브리핑·why 표시용."""
    rows = metrics.load_all(root)
    findings = core_ledger.list_findings(root)
    out = []
    for a in core_ledger.list_actions(root):
        if a and a.get("status") in OPEN:
            _, reason, nums = evaluate(root, a, findings, rows)
            out.append({"action_id": a["action_id"], "layer": a.get("layer"), "target": a.get("target"),
                        "status": a.get("status"), "deadline": deadline_of(a, root).isoformat(timespec="seconds"),
                        **nums})
    return out


def _sync_gate(root, action, status):
    gid = (action.get("target") or {}).get("gate_id")
    if gid:
        from vmc_harness.ledger import revert
        revert.set_gate_status(root, gid, status)


# ---- 옛 판정 바로잡기(업데이트 뒤 첫 판정 한 번) ----

MIGRATION = "exposure-v2"


def migrate(root, at=None):
    """0.1.4까지 조건 없는 게이트는 모든 PreToolUse를 노출로 셌다. 그런 게이트의 존속 중 실제로 막은 횟수가
    judge.min_hits 미만인 것은 시험 중으로 되돌린다(조건 있는 게이트는 옛 노출도 조건 해당이라 그대로). 시험 중 게이트 조치는 옛 노출을 빼고 다음 날부터 다시 센다.
    원복된 게이트 파일이 켜진 채(enabled true) 남은 것, 존속인데 게이트 파일은 trial인 것도 맞춘다. 반환: 되돌린 action_id 목록."""
    at = at or now()
    st = read_json(_state_file(root), {}) or {}
    if MIGRATION in (st.get("migrations") or []):
        return []
    from vmc_harness.core import events
    from vmc_harness.ledger import revert
    rows = metrics.load_all(root)
    min_hits = config.get("judge.min_hits", root)
    count_from = (at + _dt.timedelta(days=1)).strftime("%Y-%m-%d")
    deadline = (at + _dt.timedelta(days=config.get("judge.max_days", root))).isoformat(timespec="seconds")
    verdicts = {v.get("action_id"): v for v in core_ledger.list_verdicts(root) if v}
    reopened = []
    for a in core_ledger.list_actions(root):
        gid = ((a or {}).get("target") or {}).get("gate_id")
        if not gid:
            continue
        status = a.get("status")
        meta = read_json(paths.harness_dir(root) / "gates" / ("%s.json" % gid), None)
        tr = ((meta or {}).get("trigger") or {}) if isinstance(meta, dict) else {}
        old_def = isinstance(meta, dict) and not (tr.get("paths") or tr.get("change_kinds"))  # 조건 있는 게이트는 옛 노출도 조건 해당
        if status == "kept":
            blocked = metrics.gate_totals(root, gid, (a.get("created_at") or "")[:10] or None, rows)["blocked"]
            if not old_def or blocked >= min_hits:
                revert.set_gate_status(root, gid, "kept")
                continue
            trial = dict(a.get("trial") or {})
            trial.update({"started_at": at.isoformat(timespec="seconds"), "count_from": count_from,
                          "deadline": deadline, "exposures": 0,
                          "reopened": {"from": "kept", "at": at.isoformat(timespec="seconds"),
                                       "reason": "옛 노출 정의(모든 도구 호출)로 난 존속 — 실제로 막은 횟수 %d" % blocked}})
            core_ledger.update_action(root, a["action_id"], status="trial", trial=trial)
            revert.set_gate_status(root, gid, "trial")
            v = verdicts.get(a["action_id"])
            if v:
                v = dict(v, superseded={"at": at.isoformat(timespec="seconds"), "result": "trial",
                                        "reason": "근거 부족(실제로 막은 횟수 %d < %d) — 시험 재개" % (blocked, min_hits)})
                core_ledger.write_verdict(root, v)
            events.log(root, "verdict_reopened", action_id=a["action_id"], gate_id=gid, blocked=blocked)
            reopened.append(a["action_id"])
        elif status in OPEN:
            trial = dict(a.get("trial") or {})
            if not old_def:
                continue
            since = (a.get("created_at") or "")[:10] or None
            inflated = metrics.exposures(root, a["action_id"], since, rows) > \
                metrics.gate_totals(root, gid, since, rows)["blocked"]
            if inflated and not trial.get("count_from"):
                trial.update({"count_from": count_from, "started_at": at.isoformat(timespec="seconds")})
                if (parse_iso(trial.get("deadline")) or deadline_of(a, root)) < parse_iso(deadline):
                    trial["deadline"] = deadline
                core_ledger.update_action(root, a["action_id"], trial=trial)
        elif status == "reverted":
            revert.set_gate_status(root, gid, "reverted")  # enabled false까지 맞춘다
    st = read_json(_state_file(root), {}) or {}
    st["migrations"] = sorted(set(st.get("migrations") or []) | {MIGRATION})
    write_json(_state_file(root), st)
    return reopened


# ---- 세션 시작 지연 실행 · 밤 루프 ----
# judge-state.json: last_run(마지막 완료) · spawned_at(분리 실행·밤 루프가 시작을 맡은 시각) · started_at(분리 실행 본체가 실제로
# 시작한 시각) · summary · migrations. spawned_at이 last_run보다 새로운데 judge.spawn_grace_min이 지나도 안 끝났으면 실패로 본다.

def _state_file(root):
    return local_dir(root) / "judge-state.json"


def _state(root):
    return read_json(_state_file(root), {}) or {}


def _update_state(root, **fields):
    st = _state(root)
    st.update(fields)
    write_json(_state_file(root), st)
    return st


def due(root):
    """지금 판정 한 바퀴를 돌릴 차례인가. 맡겨 둔 실행이 시간 안에 안 끝났으면 실패로 기록하고 True."""
    st = _state(root)
    last = parse_iso(st.get("last_run"))
    spawned = parse_iso(st.get("spawned_at"))
    t = now()
    if spawned and (not last or spawned > last):
        if (t - spawned).total_seconds() < config.get("judge.spawn_grace_min", root) * 60:
            return False  # 도는 중
        if st.get("failure_logged") != st.get("spawned_at"):
            started = parse_iso(st.get("started_at"))
            why = ("분리 실행이 시작했지만 끝나지 않음(중간에 죽음)" if started and started >= spawned
                   else "분리 실행이 시작되지 않음(프로세스 기동 실패)")
            _update_state(root, failure_logged=st.get("spawned_at"))
            from vmc_harness.ledger import health
            health.record_failure(root, st.get("spawned_by") or "session_start", why)
        return True
    return not last or (t - last).total_seconds() >= config.get("judge.min_interval_h", root) * 3600


def mark_spawned(root, by="session_start"):
    _update_state(root, spawned_at=now_iso(), spawned_by=by)


def mark_started(root):
    _update_state(root, started_at=now_iso())


def mark_run(root, summary=None):
    _update_state(root, last_run=now_iso(), summary=summary or {})


def run(root, at=None):
    """판정 한 바퀴: 집계 → 옛 판정 바로잡기(한 번) → 사람 git revert 감지 → 미룬 원복 → 판정 → 승격 확인.
    반환: 이번에 난 verdict 목록."""
    from vmc_harness.ledger import health
    metrics.rollup(root)
    health.mark(root, "rollup_last_run")
    summary = {}
    reopened = migrate(root, at)
    if reopened:
        summary["reopened"] = reopened
    try:
        from vmc_harness.ledger import git_watch
        summary["revert_findings"] = len(git_watch.scan(root))
    except Exception as e:  # 편의 장치 — 판정은 계속
        summary["git_watch_error"] = str(e)[:200]
    from vmc_harness.ledger import revert
    summary["retried"] = revert.retry_pending(root)
    verdicts = judge_actions(root, at)
    summary["verdicts"] = len(verdicts)
    try:
        from vmc_harness.brain import promote
        summary["promoted"] = promote.check(root)
    except ImportError:
        pass
    mark_run(root, summary)
    health.mark(root, "judge_last_run")
    return verdicts
