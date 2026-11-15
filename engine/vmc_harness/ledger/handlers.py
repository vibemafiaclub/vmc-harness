"""ledger 훅 핸들러 (hooks/contract.py). 편의 장치 — 예외는 디스패처가 통과 처리(fail-open).

- SessionStart: 규칙 노출(세션당 1회) + 하루 1회 정도 판정 한 바퀴를 분리 실행(훅은 기다리지 않는다) + 학습 건강 점검.
  분리 실행이 기동·완료되지 않으면 다음 판단 때 실패로 기록한다(judge.due). 밤 루프 tick도 같은 판정을 돌린다(loop/tick.py)
  분리 실행 = 집계 → 사람 git revert 감지 → 미룬 원복 → 판정 → 승격 → (SessionEnd가 없던) 지난 세션 자가 점검 → 낡은 문서 검출
- PostToolUse: 클렌징 변경 영역 작업 노출(세션·조치당 1회), 낡은 문서·그 영역을 만지면 한 줄 알림
- SessionEnd: 자가 점검 분리 실행(SessionEnd마다 — 지난 점검 뒤 새 구간만)
"""

import os
import sys
from pathlib import Path

from vmc_harness.core import llm
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.project import local_dir

READ_TOOLS = ("Read", "view", "read_file")


def spawn_cli(args, cwd=None):
    """`python -m vmc_harness <args>`를 분리 실행. 엔진 경로를 PYTHONPATH로 넘긴다(설치 위치와 무관하게)."""
    from vmc_harness.core.proc import spawn_detached
    pkg_parent = str(Path(__file__).resolve().parent.parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return spawn_detached([sys.executable, "-m", "vmc_harness"] + list(args), cwd=cwd, env=env)


def _state(root):
    return local_dir(root) / "ledger-hook-state.json"


def _seen(root, key):
    """key를 처음 보면 True(기록), 이미 봤으면 False."""
    p = _state(root)
    st = read_json(p, {}) or {}
    seen = st.get("seen", [])
    if key in seen:
        return False
    seen.append(key)
    st["seen"] = seen[-5000:]
    write_json(p, st)
    return True


def _rule_exposures(root, session_id):
    from vmc_harness.core import ledger as core_ledger
    from vmc_harness.ledger import exposure
    from vmc_harness.core import events
    from vmc_harness.ledger.why import _rule_id, _rule_text, load_rules, rule_active
    rules = [r for r in load_rules(root) if rule_active(r)]
    active = {_rule_id(r) for r in rules}
    if rules and _seen(root, "rules_loaded:%s" % session_id):
        # why의 재료(09 events — 세션별 로드된 규칙). 원문은 rules.json에 있으니 id·해시만
        events.log(root, "rules_loaded", session_id=session_id, rule_ids=sorted(active),
                   hashes=[core_ledger.sha(_rule_text(r)) for r in rules])
    for a in core_ledger.list_actions(root):
        rid = ((a or {}).get("target") or {}).get("rule_id")
        if rid and rid in active and a.get("status") in exposure.OPEN and _seen(root, "rule:%s:%s" % (session_id, rid)):
            exposure.record(root, rule_id=rid, session_id=session_id)


def on_session_start(ctx):
    if llm.nested() or not ctx.root:
        return None
    if ctx.session_id:
        _rule_exposures(ctx.root, ctx.session_id)
    from vmc_harness.ledger import health, judge
    if judge.due(ctx.root):
        judge.mark_spawned(ctx.root, "session_start")
        args = ["judge", "--background"]
        if ctx.session_id:
            args += ["--exclude-session", ctx.session_id]
        try:
            spawn_cli(args, cwd=str(ctx.root))
        except OSError as e:
            health.record_failure(ctx.root, "session_start", "분리 실행 기동 실패: %s" % e)
    health.check(ctx.root)
    return None


def _touched(ctx):
    paths_ = list(ctx.write_paths or [])
    if ctx.tool_name in READ_TOOLS:
        p = (ctx.tool_input or {}).get("file_path") or (ctx.tool_input or {}).get("path")
        if p:
            try:
                p = str(Path(p).resolve().relative_to(Path(ctx.root).resolve())).replace("\\", "/")
            except ValueError:
                p = None
            if p:
                paths_.append(p)
    return paths_


def on_post_tool(ctx):
    if llm.nested() or not ctx.root:
        return None
    touched = _touched(ctx)
    if not touched:
        return None
    from vmc_harness.core import ledger as core_ledger
    from vmc_harness.ledger import exposure, stale_docs
    if ctx.write_paths:
        area_actions = [a for a in core_ledger.list_actions(ctx.root)
                        if a and a.get("status") in exposure.OPEN and (a.get("target") or {}).get("area")]
        for a in area_actions:
            if any(exposure.in_area(p, a["target"]["area"]) for p in ctx.write_paths):
                if _seen(ctx.root, "area:%s:%s" % (ctx.session_id or ctx.task_id, a["action_id"])):
                    exposure.record(ctx.root, action_id=a["action_id"], session_id=ctx.session_id)
    stale_docs.notice_for(ctx.root, touched)
    return None


def on_session_end(ctx):
    if llm.nested() or not ctx.root or not ctx.transcript_path:
        return None
    # 세션마다가 아니라 SessionEnd마다 — 이어 쓴 세션(--resume)은 턴마다 끝나므로 새 구간을 그때그때 본다.
    # 이미 본 구간을 다시 세지 않는 건 selfcheck 커서가 맡는다(selfcheck-done.json)
    args = ["selfcheck", "--session", ctx.transcript_path, "--agent", ctx.agent or "claude"]
    if ctx.session_id:
        args += ["--session-id", ctx.session_id]
    try:
        spawn_cli(args, cwd=str(ctx.root))
    except OSError:
        pass
    return None


def background_job(root, exclude_session=None):
    """분리 실행 본체(`vmc-harness judge --background`)."""
    out = {}
    from vmc_harness.ledger import health, judge
    judge.mark_started(root)
    try:
        out["verdicts"] = [v["action_id"] + ":" + v["result"] for v in judge.run(root)]
    except Exception as e:
        out["judge_error"] = str(e)[:200]
        health.record_failure(root, "judge", e)
    try:
        from vmc_harness.ledger import selfcheck
        out["selfcheck"] = len(selfcheck.run_pending(root, exclude_session))
    except Exception as e:
        out["selfcheck_error"] = str(e)[:200]
        health.record_failure(root, "selfcheck", e)
    try:
        from vmc_harness.ledger import stale_docs
        out["stale_docs"] = len(stale_docs.scan(root).get("docs") or {})
    except Exception as e:
        out["stale_error"] = str(e)[:200]
    try:
        from vmc_harness.ledger import metrics
        metrics.rollup(root)
        health.mark(root, "rollup_last_run")
    except Exception as e:
        health.record_failure(root, "rollup", e)
    judge.mark_run(root, out)
    health.check(root)
    return out
