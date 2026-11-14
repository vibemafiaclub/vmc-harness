"""완료 게이트 (spec 02 — Stop, 안전).

이번 작업에서 페이지·API·DB 동작이 바뀌었는데 실제 동작 증거(하네스 마커)가 마지막 편집 이후에 없으면 종료를 막고 빠진 것만 알린다.
- "이번 작업이 바꾼 것"은 task.scope.touched(착수 스냅샷 이후 바뀐 파일 중 이 작업이 쓴 것 — 모든 게이트 공용, 하네스가 쓴 파일·착수 전 변경·다른 세션 쓰기 제외)
- 증거를 무효로 만드는 "마지막 편집"은 동작 파일(page·api·db, 영향 반경이면 그 소스)의 편집만 — 문서·다른 파일 편집은 아니다(현장 결함 C5)
- 증거보다 수정 시각이 늦어도, 증거가 이 세션의 마지막 도구 쓰기(state.write_ns) 뒤에 만들어졌고 내용이 증거를 남긴 순간과 같으면 인정한다
  (마커의 tree_sha — 다른 세션의 git autostash가 같은 내용을 다시 써 시각만 늦춘 경우, 2026-11-15)
- 발동 조건: 바뀐 파일 중 동작 파일(page·api·db glob)이 있을 때. 중·대 작업은 영향 반경(impact)이 페이지·API에 닿아도 발동.
  소 작업은 직접 바뀐 동작 파일만 본다(페이지·DB가 안 바뀐 작업엔 발동하지 않음)
- 인정: screenshot·api_call·db_dryrun·test_run(기준선 대비 새 실패 0건)·qa_scenario
- 결재 흐름(보호 영역 kind=approval_flow)에 닿으면 qa_scenario 증거 필수(안전선)
- 같은 작업에서 막는 횟수 상한(gates.completion_max_blocks) — 넘으면 통과시키고 사람에게 꼭 알릴 일로 남긴다(무한 정지 방지)
"""

import hashlib
import json
import os
from pathlib import Path

from vmc_harness.core import config
from vmc_harness.gates import evidence, globs, protected, state
from vmc_harness.gates.common import behavior_globs, behavior_kind, block, log_event, notify, short_list

NAME = "completion"
LABEL = {"page": "페이지", "api": "API", "db": "DB"}


def changed(root, session_id, task):
    """이번 작업이 바꾼 파일 — 이 작업(선언이 없으면 이 세션)이 쓴 것만. 원본은 task.scope.touched.
    같은 작업 트리의 다른 세션·사람이 쓴 파일로 막지 않는다(현장 결함 — 동시 세션 오탐)."""
    from vmc_harness.task import scope
    if task and isinstance(task.get("base"), dict):
        return scope.touched(root, task)
    st = state.load(root, session_id)
    mine = set(st.get("writes") or [])
    if not mine:
        return []
    return [f for f in scope.changes(root, {"head": st.get("base_head"), "since": st.get("session_started_at")},
                                     edits=sorted(mine)) if f in mine]


def _mtime(root, rel):
    try:
        import datetime as _dt
        return _dt.datetime.fromtimestamp((Path(root) / rel).stat().st_mtime).astimezone()
    except OSError:
        return None


def file_sha(root, rel):
    try:
        h = hashlib.sha256()
        with open(Path(root) / rel, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def own_write_time(root, session_id, files):
    """이 세션이 도구로 그 파일들을 마지막으로 쓴 시각(state.write_ns). 하나라도 기록이 없으면 None —
    스크립트 쓰기 등 기록 밖 편집이 있을 수 있으니 내용 비교로 인정하지 않는다."""
    import datetime as _dt
    w = state.load(root, session_id).get("write_ns") or {}
    ns = [w.get(f) for f in files]
    if not ns or not all(isinstance(x, int) for x in ns):
        return None
    return _dt.datetime.fromtimestamp(max(ns) / 1000 / 1000 / 1000).astimezone()  # ns → 초


def same_content(marker, current):
    """증거를 남긴 순간과 지금, 동작 파일들의 내용이 모두 같은가 — 시각만 늦어진 파일(다른 세션의 git autostash 등)로
    증거를 무효로 보지 않는다(2026-11-15 현장). 증거에 해시가 없는(옛) 마커·적지 못한 파일은 False."""
    snap = marker.get("tree_sha")
    if not isinstance(snap, dict) or not current:
        return False
    return all(sha is not None and snap.get(f) == sha for f, sha in current.items())


def last_edit(root, files):
    """동작 파일들의 마지막 수정 시각(없으면 None)."""
    times = [t for t in (_mtime(root, f) for f in files) if t]
    return max(times) if times else None


def _impact_reach(root, session_id, files):
    """중·대 작업: 바뀐 소스 파일의 영향 반경이 페이지·API·DB에 닿는가(캐시)."""
    from vmc_harness.gates import impact
    key = hashlib.sha256(json.dumps([(f, str(_mtime(root, f))) for f in files]).encode("utf-8")).hexdigest()[:16]
    st = state.load(root, session_id)
    cached = (st.get("impact_cache") or {})
    if cached.get("key") == key:
        return cached.get("reach") or []
    reach = []
    srcs = [f for f in files if f.endswith(impact.PY_EXT + impact.JS_EXT)]
    if srcs:
        all_files = impact.source_files(root)
        for f in srcs:
            r = impact.impact(root, f, files=all_files)
            reach.extend(r["pages"] + r["apis"] + r["db"])
    reach = sorted(set(reach))
    st = state.load(root, session_id)
    st["impact_cache"] = {"key": key, "reach": reach}
    state.save(root, session_id, st)
    return reach


def evaluate(root, session_id, task):
    """막아야 하면 {'missing': [...], 'behavior': {...}} 아니면 None. 판정만(부작용 없음 — 캐시 제외)."""
    files = changed(root, session_id, task)
    if not files:
        return None
    bg = behavior_globs(root)
    beh = {}
    for f in files:
        k = behavior_kind(root, f, bg)
        if k:
            beh.setdefault(k, []).append(f)
    size = (task or {}).get("size") or "mid"
    beh_files = sorted({f for v in beh.values() for f in v})
    if not beh and size != "small":
        reach = _impact_reach(root, session_id, files)
        if reach:
            beh["impact"] = reach
            from vmc_harness.gates import impact
            beh_files = [f for f in files if f.endswith(impact.PY_EXT + impact.JS_EXT)]
    if not beh:
        return None
    tkey = state.task_key(root, session_id, (task or {}).get("task_id"))
    edit_at = last_edit(root, beh_files)
    markers = evidence.list_markers(root, tkey)
    current = {f: file_sha(root, f) for f in beh_files}
    own_at = own_write_time(root, session_id, beh_files)

    def _fresh(m):
        t = evidence.marker_time(m)
        if edit_at is None or (t and t >= edit_at):
            return True
        # 수정 시각만 늦어진 경우(다른 세션의 git autostash 등): 이 세션의 마지막 쓰기 뒤에 만든 증거이고,
        # 증거를 남긴 순간과 지금 내용이 같으면 인정(2026-11-15 현장). 옛 스크린샷 재사용 방지는 그대로(t ≥ 이 세션의 쓰기).
        return bool(t and own_at and t >= own_at and same_content(m, current))
    fresh = [m for m in markers if _fresh(m)]
    good = [m for m in fresh if evidence.marker_ok(m)]
    missing = []
    if not good:
        parts = ["%s: %s" % (LABEL.get(k, "영향 받는 페이지·API"), short_list(v)) for k, v in sorted(beh.items())]
        bad_tests = [m for m in fresh if m.get("kind") == "test_run" and not evidence.marker_ok(m)]
        line = "바뀐 동작(%s)에 대한 마지막 편집 이후 실제 동작 증거 없음" % "; ".join(parts)
        if bad_tests:
            s = bad_tests[-1].get("summary") or {}
            line += " — 테스트 실행 기록은 기준선 대비 새 실패가 있어 증거로 인정 안 됨(새 실패 %s건)" % (
                s.get("new_failures") if not s.get("unparsed_failure") else "판독 불가")
        missing.append(line)
    af = protected.approval_flow_globs(root)
    flow_hit = [f for f in files if globs.match_any(f, af)]
    if flow_hit and not [m for m in good if m.get("kind") == "qa_scenario"]:
        missing.append("결재 흐름 변경(%s)에 QA 시나리오 증거 없음" % short_list(flow_hit))
    if not missing:
        return None
    return {"missing": missing, "behavior": beh, "task_key": tkey, "files": files}


def check(ctx, task):
    root = ctx.root
    res = evaluate(root, ctx.session_id, task)
    if not res:
        return None
    if state.consume_bypass(root, ctx, NAME):
        return None
    n = state.bump_completion(root, ctx.session_id, res["task_key"])
    if n > int(config.get("gates.completion_max_blocks", root)):
        try:
            log_event(root, "completion_gave_up", task_id=res["task_key"], missing=len(res["missing"]))
            if state.noticed_once(root, ctx.session_id, "completion_gave_up:%s" % res["task_key"]):
                notify(root, "gate_blocked", "must", "완료 게이트: 실제 동작 증거 없이 작업이 끝남 (%s)" % res["task_key"],
                            area=os.path.dirname(res["files"][0]) if res["files"] else None, refs={"task_id": res["task_key"]})
        except Exception:
            pass
        return None
    text = "빠진 증거 — " + " / ".join(res["missing"])
    return block(text, "빠진 증거 — 바뀐 동작에 대한 마지막 편집 이후 실제 동작 증거 없음", kind="missing_evidence")
