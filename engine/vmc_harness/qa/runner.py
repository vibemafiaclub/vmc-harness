"""QA 실행기 (spec 05) — 시나리오를 열어 에이전트에게 목표·참고·제약·출력 형식을 준다(01 P6, 절차 아님).

실제 확인(화면에서 눌러 보기·스크린샷)과 실패 수정은 에이전트(또는 vmc-qa 서브에이전트)가 한다.
여기서 하는 것: 시나리오 메타 게이트(기대값 출처), 선언된 데이터 쓰기의 data-policy 검사, 예산 시계 시작,
QA 진행 표시(`.harness/cache/qa/active.json` — 훅이 범위 밖 쓰기를 막는 근거), 결과 기록 뒤 증거 연결.
"""

import os

from vmc_harness.core import paths
from vmc_harness.core.clock import now_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.qa import policy, results, scenarios


class RunRefused(Exception):
    """시나리오를 시작하지 않는 이유(무엇이 빠졌는지·무엇을 어겼는지만)."""


def active_path(root):
    return paths.cache_dir(root) / "qa" / "active.json"


def active(root):
    return read_json(active_path(root), None)


def _set_active(root, sid):
    write_json(active_path(root), {"scenario_id": sid, "since": now_iso(), "pid": os.getpid()})


def clear_active(root, sid=None):
    a = active(root)
    if a and (sid is None or a.get("scenario_id") == sid):
        try:
            active_path(root).unlink()
        except OSError:
            pass


def next_id(root):
    nxt = results.resume(root)["next"]
    return nxt["id"] if nxt else None


def start(root, sid=None):
    """시나리오 시작. (scenario, result_rec, policy) — 거부면 RunRefused."""
    sid = sid or next_id(root)
    if not sid:
        raise RunRefused("돌릴 시나리오 없음")
    sc = scenarios.load(root, sid)
    if sc is None:
        raise RunRefused("시나리오 없음: %s" % sid)
    ok, reason = scenarios.is_valid(sc)
    if not ok:
        raise RunRefused(reason)
    pol = policy.ensure(root)
    for w in (sc.get("data") or {}).get("writes") or []:
        ok, reason = policy.check_data_write(pol, w.get("entity"), w.get("op"), w.get("record"))
        if not ok:
            raise RunRefused("시나리오 %s: %s" % (sid, reason))
    rec = results.begin(root, sid)
    if rec.get("status") == "HOLD" and (rec.get("budget") or {}).get("exceeded"):
        clear_active(root, sid)
        raise RunRefused("시나리오 %s: HOLD(사람 확인 대기)" % sid)
    _set_active(root, sid)
    return sc, rec, pol


def brief_text(root, sc, pol):
    """에이전트에게 보이는 문구 — 목표·참고·제약·출력 형식. 예산·숫자 없음(01 P5)."""
    sid = sc["id"]
    lines = ["QA 시나리오 %s — %s" % (sid, sc.get("title") or "")]
    if sc.get("pages"):
        lines.append("페이지: %s" % ", ".join(sc["pages"]))
    lines.append("")
    lines.append("목표: 아래 기대값이 실제 화면에서 맞는지 확인한다. 맞지 않으면 원인을 찾아 고치고 다시 확인한다. "
                 "사람 결정이나 DB 변경이 필요 없는 실패는 사람에게 묻지 않는다. "
                 "이 시나리오는 PASS가 되거나, 사람 결정·DB 변경이 필요해 HOLD가 되면 끝난다.")
    lines.append("")
    lines.append("기대값(출처):")
    for e in sc["expectations"]:
        src = e["source"]
        tag = "사람 지시 \"%s\"" % src.get("quote") if src["type"] == "human_quote" else "현재 동작 %s" % src.get("ref")
        lines.append("  - %s  [%s]" % (e["text"], tag))
    if sc.get("steps"):
        lines.append("")
        lines.append("참고(이전에 쓴 흐름 — 따를 의무 없음):")
        for s in sc["steps"]:
            lines.append("  - %s" % s)
    if sc.get("account"):
        lines.append("")
        lines.append("계정: %s (비밀번호는 `vmc-harness qa accounts copy %s`로 클립보드에 — 값은 출력되지 않는다)"
                     % (sc["account"], sc["account"]))
    lines.append("")
    lines.append("제약:")
    lines.append("  - DB는 화면으로만 바꾼다. DB 직접 접속은 막힌다")
    allowed = ["%s(%s)" % (a.get("entity"), ",".join(a.get("ops") or [])) for a in pol.get("allowed") or []]
    forbidden = [str(f.get("entity")) for f in pol.get("forbidden") or []]
    lines.append("  - 바꿔도 되는 데이터: %s" % (", ".join(allowed) if allowed else "없음"))
    if forbidden:
        lines.append("  - 금지 데이터: %s" % ", ".join(forbidden))
    lines.append("  - 시나리오·기대값은 고치지 않는다(새 기대값은 출처와 함께 새 시나리오로)")
    lines.append("  - 코드 수정은 평소 작업과 같다(착수 선언·완료 게이트)")
    lines.append("")
    lines.append("쓸 수 있는 것: 브라우저 도구(평소 쓰던 것), `vmc-harness evidence screenshot` (스크린샷 증거), "
                 "`vmc-harness impact` (영향 반경), vmc-qa 서브에이전트(쓸지는 판단)")
    lines.append("")
    lines.append("결과 기록 형식:")
    lines.append("  vmc-harness qa status set %s PASS --evidence <증거 경로>" % sid)
    lines.append("  vmc-harness qa status set %s FAIL|HOLD --cause \"<원인>\" --needs-human yes|no --needs-db-change yes|no "
                 "[--evidence <경로>]" % sid)
    return "\n".join(lines)


def _task_for_evidence(root):
    tid = os.environ.get("VMC_HARNESS_TASK_ID")
    if tid:
        return tid
    try:
        # 완료 게이트와 같은 키로(gates.state.task_key — 착수 선언이 있으면 task_id, 없으면 S-<세션>)
        from vmc_harness.gates import state as gstate
        from vmc_harness.task import state
        return gstate.task_key(root, state.resolve_session_id(root))
    except Exception:
        return None


def finish(root, sid, status, cause=None, needs_human=None, needs_db_change=None, evidence=None,
           by="agent", reset_budget=False):
    """결과 기록(= `qa status set`). PASS면 지금 작업에 `qa_scenario` 증거 마커를 남긴다(gates 증거 계약)."""
    rec = results.set_status(root, sid, status, cause, needs_human, needs_db_change, evidence, by, reset_budget)
    clear_active(root, sid)
    if rec.get("status") == "PASS" and by == "agent":
        tid = _task_for_evidence(root)
        if tid:
            try:
                from vmc_harness.gates import evidence as ev
                ev.make(root, tid, "qa_scenario", sid,
                        data={"scenario_id": sid, "status": "PASS", "evidence": rec.get("evidence") or []})
            except (ImportError, AttributeError):
                pass
            except Exception:
                pass
    return rec
