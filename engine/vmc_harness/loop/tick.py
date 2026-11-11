"""루프 진입점 (spec 06) — 스케줄러가 `loop.check_interval_min`마다 `loop tick`, 사람이 `start-loop`.

tick: 장부 관리(upkeep — 판정 차례면 집계·판정, 학습 건강 점검, 제안 후보 만료) → 퇴근 판정(start-loop 요청 또는
      start_hour 이후 idle_hours 무지시) → run.
run: 프로젝트당 1개(dist 원격 잠금 + 로컬 실행 표시) → 후보 발견(밤당 1회) → 티켓 동기화(기준선 실패 테스트·진단 제안,
     LLM 없음 — loop/tickets.py) → 우선순위 순 처리(실패 테스트 복구 → 진단 티켓 → 클렌징) →
     중단 신호(사람 복귀)·stop_hour·밤당 토큰 상한에서 멈춘다. 도는 동안 OS 절전을 막는다(power). 기록은 로컬 원본 `loop-runs.jsonl`.
"""


from vmc_harness.core import config, llm
from vmc_harness.core.clock import now, now_iso
from vmc_harness.core.jsonio import append_jsonl, read_jsonl
from vmc_harness.core.project import local_dir
from vmc_harness.loop import activity, candidates, cleanse, gitops, power, tickets


def runs_path(root):
    return local_dir(root) / "loop-runs.jsonl"


def log(root, event, **fields):
    from vmc_harness import KERNEL_VERSION
    rec = {"at": now_iso(), "event": event, "night": activity.night_key(root), "kernel_version": KERNEL_VERSION}
    rec.update(fields)
    append_jsonl(runs_path(root), rec)
    return rec


def night_records(root, key=None):
    key = key or activity.night_key(root)
    return [r for r in read_jsonl(runs_path(root)) if r.get("night") == key]


def night_tokens(root, key=None):
    return sum(int(r.get("tokens") or 0) for r in night_records(root, key) if r.get("event") in ("unit", "discover"))


def _done_tonight(root):
    """오늘 밤 이미 끝까지 돈 경우(대기열 소진·상한·마감) — 같은 밤 재시작 안 함. 양보로 끝났으면 다시 돈다."""
    ends = [r for r in night_records(root) if r.get("event") == "end"]
    return bool(ends) and ends[-1].get("reason") in ("done", "token_cap", "stop_hour", "auth_failed")


def tick(root):
    """{"ran": bool, "reason": str}"""
    if llm.nested():
        return {"ran": False, "reason": "nested"}
    upkeep(root)
    if not config.get("loop.enabled", root):
        return {"ran": False, "reason": "disabled"}
    if activity.is_running(root):
        return {"ran": False, "reason": "running"}
    if activity.start_requested(root):
        trigger = "start-loop"
    elif activity.is_idle(root):
        trigger = "idle"
    else:
        return {"ran": False, "reason": "not_idle"}
    if _done_tonight(root):
        return {"ran": False, "reason": "done_tonight"}
    if trigger == "idle" and not activity.recently_used(root):
        # 아무도 쓰지 않는 프로젝트 — 퇴근 판정만으로 밤마다 돌지 않는다(start-loop는 사람이 시킨 것이라 예외). 밤당 한 줄만 남긴다
        if not any(r.get("event") == "skip" for r in night_records(root)):
            last = activity.last_human_at(root)
            log(root, "skip", reason="no_recent_human", last_human_at=last.isoformat(timespec="seconds") if last else None)
        return {"ran": False, "reason": "no_recent_human"}
    return run(root, trigger)


def upkeep(root):
    """세션 시작 분리 실행 한 경로에만 기대지 않는다 — 스케줄러가 부를 때마다(판정 차례일 때만) 집계·판정을 직접 돌리고,
    학습 건강을 점검하고, 손대지 않은 제안 후보를 만료한다. 실패는 events·알림으로 남기고 루프는 계속."""
    from vmc_harness.ledger import health, judge
    out = {}
    try:
        if judge.due(root):
            judge.mark_spawned(root, "loop_tick")
            judge.mark_started(root)
            out["verdicts"] = len(judge.run(root))
    except Exception as e:
        out["judge_error"] = str(e)[:200]
        health.record_failure(root, "loop_tick", e)
    try:  # 예측 학습 루프(spec 13) — LLM 없이 원인 판정·티켓. 꺼져 있으면 아무것도 안 한다
        from vmc_harness.task import analysis
        out["predict"] = analysis.run(root)
    except Exception as e:
        health.record_failure(root, "predict_loop", e)
    try:
        out["expired"] = candidates.expire_proposed(root)
    except Exception as e:
        health.record_failure(root, "expire_proposed", e)
    try:
        out["stalled"] = health.check(root).get("stalled")
    except Exception:
        pass
    return out


def _acquire(root):
    ttl = config.get("loop.lock_ttl_s", root)
    try:
        from vmc_harness.dist import lock
        if not lock.acquire(root, ttl):
            return False, None
        return True, lock
    except (ImportError, AttributeError):
        return True, None
    except Exception:
        return True, None  # 원격 잠금 서버에 못 닿으면 로컬 표시만으로(편의 — 다른 PC 동시 실행은 worktree가 PC마다 따로라 안전)


def run(root, trigger="manual"):
    import os
    if not config.get("loop.enabled", root):
        return {"ran": False, "reason": "disabled"}
    if activity.is_running(root):
        return {"ran": False, "reason": "running"}
    ok, remote = _acquire(root)
    if not ok:
        return {"ran": False, "reason": "locked_elsewhere"}
    activity.mark_running(root, {"pid": os.getpid(), "trigger": trigger})
    activity.clear_stop(root)
    hold = power.inhibit()  # 도는 동안 OS 절전 막기 — 실패해도 진행, 결과만 기록
    deadline = activity.next_stop_at(root)
    cap = config.get("loop.nightly_tokens", root)
    log(root, "start", trigger=trigger, deadline=deadline.isoformat(timespec="seconds"), power=power.summary(hold))
    reason, units = "done", 0
    try:
        __import__("vmc_harness.task.lifecycle", fromlist=["x"]).nightly(root)  # 작업 마감 판정·밀린 실측(task)
        main = gitops.main_branch(root)
        if not any(r.get("event") == "discover" for r in night_records(root)):
            reason = _discover(root, main, deadline) or reason
        if reason == "done" and not any(r.get("event") == "sync" for r in night_records(root)):
            try:  # 기준선 실패 테스트·진단 제안 → 티켓(LLM 없음)
                log(root, "sync", **tickets.sync(root))
            except Exception as e:
                log(root, "sync", error=str(e)[:300])
            try:  # 전제조건 때문에 꺼 둔 진단 장치 — 조건이 맞으면 켠다(bootstrap.requires)
                from vmc_harness.bootstrap import requires
                rq = requires.enable_ready(root)
                if rq["enabled"] or rq["waiting"]:
                    log(root, "requires", **rq)
            except Exception as e:
                log(root, "requires", error=str(e)[:300])
        while reason == "done":
            if activity.stop_requested(root):
                reason = "yielded"
                break
            if now() >= deadline:
                reason = "stop_hour"
                break
            if night_tokens(root) >= cap:
                reason = "token_cap"
                break
            queue = [c for c in candidates.prioritized(root) if not _tried_tonight(root, c["candidate_id"])]
            if not queue:
                break
            cand = queue[0]
            activity.heartbeat(root)
            res = _process(root, cand, deadline)
            units += 1
            log(root, "unit", candidate_id=cand["candidate_id"], kind=cand.get("kind"), outcome=res.get("outcome"),
                reason=res.get("reason"),
                tokens=int(res.get("tokens") or 0), action_id=res.get("action_id"), grade=res.get("grade"))
            if res.get("outcome") == "yielded":
                reason = "stop_hour" if res.get("reason") == "deadline" else "yielded"
            elif res.get("outcome") == "failed" and _auth_problem(res.get("reason")):
                reason = "auth_failed"
    except Exception as e:  # 루프 고장이 사람 작업을 멈추지 않게 — 기록만
        reason = "error"
        log(root, "error", error=str(e)[:300])
    finally:
        log(root, "end", reason=reason, units=units, tokens=night_tokens(root))
        if reason != "yielded":
            data = activity.load(root)
            if data.get("start_requested_at"):
                data.pop("start_requested_at", None)
                from vmc_harness.core.jsonio import write_json
                write_json(activity.activity_path(root), data)
        power.release(hold)
        activity.clear_running(root)
        activity.clear_stop(root)
        if remote is not None:
            try:
                remote.release(root)
            except Exception:
                pass
    return {"ran": True, "reason": reason, "units": units}


def _process(root, cand, deadline):
    if cand.get("kind") in tickets.KINDS:
        return tickets.process(root, cand, deadline=deadline)
    return cleanse.process(root, cand, deadline=deadline)


def _tried_tonight(root, cid):
    return any(r.get("event") == "unit" and r.get("candidate_id") == cid and r.get("outcome") != "yielded"
               for r in night_records(root))


def _auth_problem(reason):
    return bool(reason) and any(k in reason for k in ("login", "auth", "credential", "로그인"))


def _discover(root, main, deadline):
    name = "discover-%s" % now().strftime("%H%M%S")
    wt, _ = gitops.add_worktree(root, name, gitops.rev(root, main) or main)
    if wt is None:
        log(root, "discover", accepted=0, tokens=0, error="worktree")
        return None
    try:
        accepted, tokens, intr = cleanse.discover(root, wt, deadline=deadline)
        log(root, "discover", accepted=accepted, tokens=tokens, interrupted=intr)
        if intr == "stop":
            return "yielded"
        if intr == "deadline":
            return "stop_hour"
        return None
    finally:
        gitops.remove_worktree(root, name)


def status(root):
    recs = read_jsonl(runs_path(root))
    from vmc_harness.core import ledger
    prepared = [a for a in ledger.list_actions(root)
                if (a.get("basis") or {}).get("type") == "cleansing" and a.get("status") == "prepared"]
    merged = [a for a in ledger.list_actions(root)
              if (a.get("basis") or {}).get("type") == "cleansing" and a.get("merged_at")]
    from vmc_harness.loop import scheduler
    return {"enabled": config.get("loop.enabled", root), "running": activity.is_running(root),
            "scheduler": scheduler.state(root), "activity": activity.load(root),
            "open_candidates": len(candidates.open_candidates(root)),
            "tickets": tickets.counts(root),
            "prepared": [{"action_id": a["action_id"], "grade": a.get("grade"), "hypothesis": a.get("hypothesis")}
                         for a in prepared],
            "merged": len(merged), "night_tokens": night_tokens(root),
            "last_runs": [r for r in recs if r.get("event") in ("start", "end")][-6:]}
