"""정기 재진단 (spec 07 §정기 재진단) — `diagnose.every` 주기(기본 하루 1회)로 진단을 다시 돌려 리포트 HTML+PDF를 새로 만든다.

- 트리거: 세션 시작 때 그 기간(하루 — 0시부터, 또는 한 주 — 월요일부터) 것이 없으면 분리된 백그라운드
  프로세스(`vmc-harness diagnose weekly`)로. 세션 시작에 띄우지 않고, 작업 중 끼어들지 않고, 모델에게 주입하지 않는다
- 조건: 직전 진단이 실제로 돌았던 프로젝트(init 진단이 모델 없이 끝났으면 사람이 `diagnose`를 한 번 돌린 뒤부터), claude CLI가 있을 때
- 입력: 이 PC 세션을 다시 읽고(상한 bootstrap.max_sessions) init 이후 실측을 더한다(diagnose.run이 remeasure로). 예산은 diagnose.budget_*
- 이미 보인·[추가]·[제외]한 제안은 다시 내지 않는다(diagnose history 그대로)
- 끝나면 알림 한 줄(notices flow). 하네스가 메일·메신저로 보내지 않는다
- 기준선도 다시 잰다(refresh_baselines): 옛 기준선 정정(Codex 자동 승인 심사 요청 — D4) → 기준선 `latest`(리포트 머리말의
  "이번 진단" 기간 — D5) → 테스트 실패 기준선(D5)
"""

import os
import sys
from pathlib import Path

from vmc_harness.core import config
from vmc_harness.core.clock import now, now_iso, parse_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.project import local_dir


def state_path(root):
    return local_dir(root) / "diagnose-weekly.json"


def _week(ref=None, root=None):
    """(기간 시작 시각, 기간 키). 상태 파일 칸 이름은 호환을 위해 week 그대로 둔다."""
    from vmc_harness.render import growth
    if config.get("diagnose.every", root) == "week":
        s = growth.week_start(ref)
        return s, growth.week_key(s)
    d = (ref or now()).astimezone()
    s = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return s, s.strftime("%Y-%m-%d")


def _alive(st, root):
    """running 기록이 살아 있나 — 진단 상한(diagnose.budget_sec)의 두 배를 넘게 멈췄으면 죽은 것."""
    if not st or st.get("status") != "running":
        return False
    at = parse_iso(st.get("started_at"))
    if at is None:
        return False
    if at.tzinfo is None:
        at = at.astimezone()
    return (now() - at).total_seconds() < 2 * config.get("diagnose.budget_sec", root)


def due(root, ref=None):
    """(이번 주에 돌려야 하나, 이유)."""
    from vmc_harness.bootstrap import diagnose
    from vmc_harness.core import llm
    if not config.get("diagnose.auto", root):
        return False, "꺼짐(diagnose.auto)"
    start, key = _week(ref, root)
    st = read_json(state_path(root), None) or {}
    if st.get("week") == key and (st.get("status") != "running" or _alive(st, root)):
        return False, "이번 기간 이미 %s" % st.get("status")
    if _alive(st, root):
        return False, "실행 중"
    prev = diagnose.load(root)
    if not isinstance(prev, dict) or prev.get("status") not in ("ok", "no_proposals"):
        return False, "직전 진단 없음"
    at = parse_iso(prev.get("generated_at"))
    if at is not None:
        if at.tzinfo is None:
            at = at.astimezone()
        if at >= start:
            return False, "이번 기간 진단 있음"
    if llm.nested() or not llm.available():
        return False, "claude CLI 없음" if not llm.available() else "중첩 호출"
    return True, None


def ensure(root, spawn=True, ref=None):
    """세션 시작 훅이 부른다. 이번 기간 재진단이 필요하면 백그라운드로 시작하고 True."""
    ok, why = due(root, ref)
    _, key = _week(ref, root)
    if why == "이번 기간 진단 있음":  # 그 기간 남은 세션 시작은 작은 상태 파일만 읽게
        write_json(state_path(root), {"week": key, "status": "recent", "at": now_iso()})
    if not ok:
        return False
    write_json(state_path(root), {"week": key, "status": "running", "started_at": now_iso(), "pid": None})
    if not spawn:
        job(root, ref=ref)
        return True
    from vmc_harness.core.proc import spawn_detached
    env = dict(os.environ)
    eng = str(Path(__file__).resolve().parents[2])
    env["PYTHONPATH"] = eng + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        spawn_detached([sys.executable, "-m", "vmc_harness", "diagnose", "weekly"], cwd=str(root), env=env)
    except Exception:
        write_json(state_path(root), {"week": key, "status": "error", "error": "백그라운드 시작 실패", "at": now_iso()})
        return False
    return True


def refresh_baselines(root, sessions, info=None, run_tests=True):
    """재진단 공통(정기·수동). sessions = 이번에 다시 읽은 이 PC 세션(없으면 None — 세션 기준선은 그대로)."""
    from vmc_harness.bootstrap import baseline, facts as bfacts
    out = {}
    if sessions:
        out["guardian"] = baseline.correct_guardian(root, sessions)
        baseline.build(root, sessions, info)
    if run_tests:
        try:
            out["tests"] = bfacts.refresh_test_baseline(root)
        except Exception as e:  # 편의 — 테스트 기준선을 못 다시 재도 진단은 돈다
            out["tests_error"] = str(e)[:200]
    return out


def changes_since(root, sessions, since):
    """지난 진단(generated_at) 뒤로 새로 생긴 입력 — {"sessions": 사람이 연 main 세션 수, "tasks": 장부 작업 수}.
    지난 진단 시각을 모르면 큰 값(다시 진단)."""
    t0 = parse_iso(since) if isinstance(since, str) else None
    if t0 is None:
        return {"sessions": float("inf"), "tasks": float("inf")}
    if t0.tzinfo is None:
        t0 = t0.astimezone()

    def after(v):
        t = parse_iso(v) if isinstance(v, str) else None
        if t is None:
            return False
        return (t if t.tzinfo else t.astimezone()) > t0
    n_sess = sum(1 for x in sessions or [] if x.get("kind") == "main" and after(x.get("started_at")))
    try:
        from vmc_harness.core import ledger
        tasks = ledger.read_tasks(root, include_open=True)
    except Exception:
        tasks = []
    n_tasks = sum(1 for r in tasks if after(r.get("started_at") or r.get("ended_at")))
    return {"sessions": n_sess, "tasks": n_tasks}


def new_signals(root, sessions, since):
    """지난 진단 뒤 새 신호 — 사람 정정(자가 점검)·게이트 차단·에이전트 반박 이벤트 + 위험 명령(diagnose.risk_commands).
    {"corrections","blocks","disputes","risk_commands","total"}. 지난 진단 시각을 모르면 total=inf."""
    import re
    from vmc_harness.bootstrap import signals
    from vmc_harness.core import events
    t0 = parse_iso(since) if isinstance(since, str) else None
    if t0 is None:
        return {"total": float("inf")}
    t0 = t0 if t0.tzinfo else t0.astimezone()

    def after(v):
        t = parse_iso(v) if isinstance(v, str) else None
        return t is not None and (t if t.tzinfo else t.astimezone()) > t0
    out = {"corrections": 0, "blocks": 0, "disputes": 0, "risk_commands": 0}
    for e in events.read(root):
        if not after(e.get("at")):
            continue
        k = e.get("kind")
        if k == "session_selfcheck":
            out["corrections"] += int(e.get("corrections") or 0)
        elif k == "gate_block":
            out["blocks"] += 1
        elif k == "block_disputed":
            out["disputes"] += 1
    rxs = []
    for x in config.get("diagnose.risk_commands", root) or []:
        try:
            rxs.append(re.compile(x))
        except re.error:
            continue
    for s in sessions or []:
        for tc in s.get("tool_calls") or []:
            if after(tc.get("at") or s.get("started_at")) and any(
                    signals.cmd_search(rx, c) for rx in rxs for c in tc.get("commands") or []):
                out["risk_commands"] += 1
    out["total"] = sum(out.values())
    return out


def job(root, ref=None, pdf=True):
    """백그라운드 본체: 세션 다시 읽기 → 진단(실측 포함) → 리포트 HTML+PDF → 알림 한 줄."""
    from vmc_harness.bootstrap import diagnose, report, sessions_input
    from vmc_harness.core import events, notices
    root = Path(root)
    _, key = _week(ref, root)
    st = {"week": key, "status": "running", "started_at": now_iso(), "pid": os.getpid()}
    write_json(state_path(root), st)
    try:
        sessions, info = sessions_input.load(root, None)
        if not info.get("main"):
            sessions = None  # 이 PC에 세션이 없으면(다른 PC 묶음으로 진단한 프로젝트) 지난 색인 그대로
        chg = changes_since(root, sessions, (diagnose.load(root) or {}).get("generated_at"))
        if chg["sessions"] < config.get("diagnose.weekly_min_new_sessions", root) \
                and chg["tasks"] < config.get("diagnose.weekly_min_new_tasks", root):
            # 지난 리포트 뒤로 입력이 거의 그대로 — 같은 진단을 다시 사지 않는다. 다음 기간에 쌓인 만큼 다시 본다
            st.update(status="skipped_no_change", ended_at=now_iso(), new_sessions=chg["sessions"], new_tasks=chg["tasks"])
            write_json(state_path(root), st)
            events.log(root, "diagnose_weekly", status="skipped_no_change", new_sessions=chg["sessions"],
                       new_tasks=chg["tasks"], tokens=0)
            return st
        sig = new_signals(root, sessions if sessions is not None else diagnose.read_sessions_index(root),
                          (diagnose.load(root) or {}).get("generated_at"))
        if sig["total"] < config.get("diagnose.weekly_min_new_signals", root):
            # 대화는 늘었어도 고칠 거리(정정·차단·반박·위험 명령)가 새로 없으면 같은 진단을 다시 사지 않는다
            st.update(status="skipped_no_signal", ended_at=now_iso(), new_signals=sig)
            write_json(state_path(root), st)
            events.log(root, "diagnose_weekly", status="skipped_no_signal", tokens=0, **{k: v for k, v in sig.items() if k != "total"})
            return st
        st["baselines"] = refresh_baselines(root, sessions, info)
        d = diagnose.run(root, sessions=sessions, trigger="weekly")
        rep = report.build(root, open_browser=False) if pdf else {"html": None, "pdf": None}
    except Exception as e:
        st.update(status="error", error=str(e)[:300], ended_at=now_iso())
        write_json(state_path(root), st)
        events.log(root, "diagnose_weekly_error", error=str(e)[:300])
        notices.add(root, "diagnosis_weekly", "flow", "진단을 다시 만들다가 멈췄어요(%s). `vmc-harness diagnose`로 다시 돌릴 수 있어요" % str(e)[:120])
        return st
    st.update(status=d.get("status"), ended_at=now_iso(), html=rep.get("html"), pdf=rep.get("pdf"),
              proposals=len(d.get("proposals") or []), user_level=len(d.get("user_level") or []),
              tokens=d.get("tokens"), sec=d.get("sec"))
    write_json(state_path(root), st)
    events.log(root, "diagnose_weekly", status=d.get("status"), proposals=st["proposals"], tokens=d.get("tokens"), sec=d.get("sec"))
    if d.get("status") in ("ok", "no_proposals"):
        notices.add(root, "diagnosis_weekly", "flow", notice_text(d, rep), refs={"html": rep.get("html"), "pdf": rep.get("pdf")},
                    buttons=["자세히", "PDF", "알림 끄기"])
    return st


def notice_text(d, rep):
    """알림 한 줄 — 숫자는 기계 결과 그대로."""
    n = len(d.get("proposals") or [])
    u = len(d.get("user_level") or [])
    parts = ["새 제안 %d개" % n]
    if u:
        parts.append("이 PC 사용자 설정 점검 %d건" % u)
    m = d.get("since_init") or {}
    g = m.get("gates") or {}
    if g.get("blocked"):
        parts.append("설치 이후 막은 장면 %d건" % g["blocked"])
    mt = m.get("metrics") or {}
    b = ((mt.get("before") or {}).get("interventions_per_session") or {}).get("median")
    c = ((mt.get("now") or {}).get("interventions_per_session") or {}).get("median")
    if b is not None and c is not None:
        parts.append("대화당 사람 개입 %s → %s(중간값)" % (b, c))
    return "진단 리포트를 다시 만들었어요 — %s. %s" % (", ".join(parts), rep.get("pdf") or rep.get("html") or "")
