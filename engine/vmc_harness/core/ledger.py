"""팀 장부 `.harness/ledger/` (spec 04·09). 기계만 쓴다 — 에이전트 손 편집은 훅이 막는다(02).

여기서 커널 원칙의 기계 강제를 한다.
- P2: finding은 사람의 명시적 행동 출처만. 학습 후보 출처 조치는 카드(L3)만
- P1: 규칙·카드 조치는 why_not_higher(200자 이하) 필수
- P3: 게이트 조치 근거는 explicit_request(원문) 또는 machine_fact(읽은 명령)만
- P4: 사람을 부르는 게이트는 사람 개입 기준(1|2|3) 필수
- 같은 대상에는 시험 중인 조치 하나만
팀 장부에는 원문·코드·비밀값을 넣지 않는다(P9) — 예외는 사람 명시 요청 원문(규칙 원문 그대로 저장, P3).
"""

import hashlib
from pathlib import Path

from vmc_harness import KERNEL_VERSION
from vmc_harness.core import paths
from vmc_harness.core.clock import now, now_iso
from vmc_harness.core.ids import new_id
from vmc_harness.core.jsonio import append_jsonl, read_json, read_jsonl, write_json
from vmc_harness.core.mask import mask
from vmc_harness.core.project import local_dir, pc_id


class MetaGateError(ValueError):
    """메타 게이트 거부. 메시지는 무엇이 빠졌는지·무엇을 어겼는지만(P6)."""


FINDING_KINDS = ("false_block", "revert", "blame", "reject", "button")
FINDING_BUTTONS = ("mute", "delete", "exclude")
BASIS_TYPES = ("explicit_request", "machine_fact", "learning", "cleansing", "finding", "predict_miss")  # predict_miss: 기계로 원인을 정한 예측 빗나감(spec 13)
LAYERS = ("structure", "gate", "rule", "card", "code")
ACTION_STATUSES = ("trial", "kept", "reverted", "extended", "prepared", "merged")
HUMAN_CRITERIA = (1, 2, 3)
# 후보 종류: 학습(LLM 해석 — 카드만) · 클렌징(재확인 조건) · 실패 테스트 복구(기준선 실패 목록 = 기계 사실) · 진단 티켓(진단 제안 → 루프 일감)
CANDIDATE_KINDS = ("learning", "cleansing", "test_repair", "ticket", "predict")  # predict: 예측 회고 티켓(spec 13)
WHY_NOT_HIGHER_MAX = 200


def _dir(root, *parts):
    return paths.ensure(paths.ledger_dir(root).joinpath(*parts))


def _stamp(rec):
    rec.setdefault("kernel_version", KERNEL_VERSION)
    return rec


def sha(text):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


# ---- 작업 기록 ----

def append_task(root, record):
    """옛 형식(월별 jsonl 한 줄 추가) — 읽기 호환용으로 남긴다. 새 기록은 write_task."""
    rec = _stamp(dict(record))
    rec.setdefault("pc_id", pc_id())
    month = now().strftime("%Y-%m")
    return append_jsonl(_dir(root, "tasks", month) / ("%s.jsonl" % rec["pc_id"]), rec)


def _safe_name(name):
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(name))


def write_task(root, record):
    """작업 기록 하나를 파일 하나로 덮어쓴다(`tasks/<YYYY-MM>/<pc_id>/<task_id>.json`, 월 = 착수 월).
    착수 선언 때 "진행 중"으로 쓰고 매 턴 갱신 — 종료 감지에 기대지 않는다(원인 ①)."""
    rec = _stamp(dict(record))
    rec["kernel_version"] = KERNEL_VERSION
    rec.setdefault("pc_id", pc_id())
    rec["written_at"] = now().isoformat()
    month = str(rec.get("started_at") or now_iso())[:7]
    write_json(_dir(root, "tasks", month, _safe_name(rec["pc_id"])) / ("%s.json" % _safe_name(rec["task_id"])), rec)
    return rec


def read_tasks(root, include_open=False, at=None):
    """작업 기록(같은 task_id는 하나로 — 파일 하나 기록이 옛 jsonl 줄보다 우선).
    "진행 중" 기록은 마지막 활동 뒤 task.finalize_idle_min이 지났으면 그 시각에 끝난 것으로 계산해 돌려준다
    (task.lifecycle.effective — 다른 PC 기록도 같은 기준). 아직 진행 중인 것은 include_open일 때만."""
    base = paths.ledger_dir(root) / "tasks"
    by_id, order = {}, []
    if base.is_dir():
        for f in sorted(base.rglob("*.jsonl")):
            for r in read_jsonl(f):
                key = r.get("task_id") or ("#%d" % len(order))
                if key not in by_id:
                    order.append(key)
                by_id[key] = r
        for f in sorted(base.rglob("*.json")):
            r = read_json(f)
            if not isinstance(r, dict) or not r.get("task_id"):
                continue
            prev = by_id.get(r["task_id"])
            if prev is None:
                order.append(r["task_id"])
            elif str(prev.get("written_at") or "") > str(r.get("written_at") or ""):
                continue
            by_id[r["task_id"]] = r
    from vmc_harness.task.lifecycle import effective
    out = []
    for key in order:
        r = effective(root, by_id[key], at=at)
        if r.get("status") == "open" and not include_open:
            continue
        out.append(r)
    out.sort(key=lambda r: str(r.get("ended_at") or r.get("last_activity_at") or r.get("started_at") or ""))
    return out


# ---- finding ----

def create_finding(root, kind, target, button=None, source_event=None):
    if kind not in FINDING_KINDS:
        raise MetaGateError("finding 출처가 사람의 명시적 행동이 아님: %s" % kind)
    if kind == "button" and button not in FINDING_BUTTONS:
        raise MetaGateError("finding 버튼 종류가 허용 목록 밖: %s" % button)
    if not isinstance(target, dict) or not any(target.get(k) for k in ("action_id", "gate_id", "commit", "card", "approval_id", "notice_kind")):
        raise MetaGateError("finding 대상 없음")
    rec = _stamp({"finding_id": new_id("finding"), "kind": kind,
                  "button": button if kind == "button" else None,
                  "target": target, "at": now_iso(), "pc_id": pc_id()})
    if source_event:
        rec["source_event"] = source_event
    write_json(_dir(root, "findings") / ("%s.json" % rec["finding_id"]), rec)
    return rec


def list_findings(root):
    return _list(root, "findings")


# ---- 후보 ----

def create_candidate(root, kind, summary, recheck=None, anchor=None, target_hash=None, quote=None, count=1):
    """kind: CANDIDATE_KINDS. quote(원문)는 로컬 quotes/에만, 장부엔 해시."""
    if kind not in CANDIDATE_KINDS:
        raise MetaGateError("후보 종류 오류: %s" % kind)
    if kind == "cleansing" and not (recheck and recheck.get("query")):
        raise MetaGateError("클렌징 후보에 재확인 조건 없음")
    if kind == "test_repair" and not (anchor or {}).get("path"):
        raise MetaGateError("실패 테스트 복구 후보에 테스트 파일 없음")
    cid = new_id("candidate")
    rec = _stamp({"candidate_id": cid, "kind": kind, "summary": mask(summary),
                  "quote_hash": sha(quote) if quote else None,
                  "recheck": recheck, "anchor": anchor, "target_hash": target_hash,
                  "count": count, "created_at": now_iso(), "status": "open"})
    if quote:
        q = local_dir(root) / "quotes" / ("%s.txt" % cid)
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_text(mask(quote), encoding="utf-8")
    write_json(_dir(root, "candidates") / ("%s.json" % cid), rec)
    return rec


def update_candidate(root, candidate_id, **fields):
    p = paths.ledger_dir(root) / "candidates" / ("%s.json" % candidate_id)
    rec = read_json(p)
    rec.update(fields)
    write_json(p, rec)
    return rec


def get_candidate(root, candidate_id):
    return read_json(paths.ledger_dir(root) / "candidates" / ("%s.json" % candidate_id))


def list_candidates(root):
    return _list(root, "candidates")


# ---- action ----

def check_action(root, basis, layer, why_not_higher=None, human_criterion=None, calls_human=False, target=None):
    """메타 게이트(조치). 통과하면 None, 아니면 MetaGateError."""
    btype = (basis or {}).get("type")
    if btype not in BASIS_TYPES:
        raise MetaGateError("조치 근거 타입 없음")
    if layer not in LAYERS:
        raise MetaGateError("조치 계층 오류: %s" % layer)
    if btype == "learning" and layer != "card":
        raise MetaGateError("학습 후보 출처 조치는 브레인 카드만 가능")
    if btype == "explicit_request" and not basis.get("quote"):
        raise MetaGateError("명시 요청 근거에 원문 없음")
    if btype == "machine_fact" and not basis.get("command"):
        raise MetaGateError("기계 사실 근거에 읽은 명령 없음")
    if btype == "predict_miss" and not (basis.get("ref") and basis.get("key")):
        raise MetaGateError("예측 빗나감 근거에 티켓·원인 키 없음")
    if layer == "gate" and btype not in ("explicit_request", "machine_fact", "finding", "predict_miss"):
        raise MetaGateError("게이트 근거는 사람 명시 요청·기계 사실뿐")
    if layer in ("rule", "card"):
        w = (why_not_higher or "").strip()
        if not w:
            raise MetaGateError("why_not_higher 없음")
        if len(w) > WHY_NOT_HIGHER_MAX:
            raise MetaGateError("why_not_higher 200자 초과")
    if calls_human and human_criterion not in HUMAN_CRITERIA:
        raise MetaGateError("사람 승인 게이트에 사람 개입 기준(1|2|3) 없음")
    if target:
        for a in list_actions(root):
            if a.get("target") == target and a.get("status") == "trial":
                raise MetaGateError("같은 대상에 시험 중인 조치가 이미 있음: %s" % a["action_id"])


def create_action(root, basis, layer, hypothesis, why_not_higher=None, commits=None,
                  human_requested=False, human_criterion=None, calls_human=False, target=None,
                  status="trial", extra=None):
    check_action(root, basis, layer, why_not_higher, human_criterion, calls_human, target)
    aid = new_id("action")
    b = dict(basis)
    if b.get("type") != "explicit_request":
        b.pop("quote", None)  # 원문은 명시 요청만 장부에(P3·P9)
    rec = _stamp({"action_id": aid, "basis": b, "layer": layer, "commits": commits or [],
                  "why_not_higher": why_not_higher, "hypothesis": hypothesis,
                  "human_requested": bool(human_requested), "human_criterion": human_criterion,
                  "target": target, "status": status, "created_at": now_iso(),
                  "trial": {"exposures": 0, "negatives": 0, "deadline": None}})
    if extra:
        rec.update(extra)
    write_json(_dir(root, "actions") / ("%s.json" % aid), rec)
    return rec


def get_action(root, action_id):
    return read_json(paths.ledger_dir(root) / "actions" / ("%s.json" % action_id))


def update_action(root, action_id, **fields):
    p = paths.ledger_dir(root) / "actions" / ("%s.json" % action_id)
    rec = read_json(p)
    if rec is None:
        raise KeyError(action_id)
    if "status" in fields and fields["status"] not in ACTION_STATUSES:
        raise MetaGateError("조치 상태 오류: %s" % fields["status"])
    rec.update(fields)
    write_json(p, rec)
    return rec


def list_actions(root):
    return _list(root, "actions")


# ---- verdict ----

def write_verdict(root, verdict):
    rec = _stamp(dict(verdict))
    write_json(_dir(root, "verdicts") / ("%s.json" % rec["action_id"]), rec)
    return rec


def list_verdicts(root):
    return _list(root, "verdicts")


# ---- approval (기계 기록 — 카드는 여기서 렌더, spec 08) ----

def create_approval(root, task_id, criterion, decision_text, recommend, reversible, revert_how,
                    verification, diff_lines, risk=None, detail=None):
    from vmc_harness.core import config
    if criterion not in HUMAN_CRITERIA:
        raise MetaGateError("승인 요청에 사람 개입 기준(1|2|3) 없음")
    if not task_id:
        raise MetaGateError("사람이 시킨 작업 밖 승인 요청(P4)")
    if not verification:
        raise MetaGateError("승인 기록에 검증 결과 없음")
    if not revert_how:
        raise MetaGateError("승인 기록에 원복 방법 없음")
    if diff_lines > config.get("approval.diff_max_lines", root):
        raise MetaGateError("승인 카드 diff 상한 초과 — 작업을 쪼갤 것")
    rec = _stamp({"approval_id": new_id("approval"), "task_id": task_id, "criterion": criterion,
                  "decision_text": decision_text, "recommend": recommend, "risk": risk,
                  "reversible": bool(reversible), "revert_how": revert_how,
                  "verification": verification, "diff_lines": diff_lines,
                  "shown_at": None, "answered_at": None, "answer": None})
    if detail:
        rec["detail"] = detail
    write_json(_dir(root, "approvals") / ("%s.json" % rec["approval_id"]), rec)
    return rec


def answer_approval(root, approval_id, answer):
    """대화창·패널 어디서 눌러도 이 기록 하나가 처리된다. 거절 = finding."""
    if answer not in ("approve", "reject"):
        raise MetaGateError("승인 응답 오류")
    p = paths.ledger_dir(root) / "approvals" / ("%s.json" % approval_id)
    rec = read_json(p)
    if rec is None:
        raise KeyError(approval_id)
    if rec.get("answer"):
        return rec
    rec["answer"] = answer
    rec["answered_at"] = now_iso()
    write_json(p, rec)
    if answer == "reject":
        create_finding(root, "reject", {"approval_id": approval_id, "action_id": rec.get("action_id")})
    return rec


def mark_approval_shown(root, approval_id):
    p = paths.ledger_dir(root) / "approvals" / ("%s.json" % approval_id)
    rec = read_json(p)
    if rec and not rec.get("shown_at"):
        rec["shown_at"] = now_iso()
        write_json(p, rec)
    return rec


def list_approvals(root):
    return _list(root, "approvals")


# ---- 일일 집계·기준선 ----

def metrics_path(root, date_str, pc=None):
    return _dir(root, "metrics", pc or pc_id()) / ("%s.json" % date_str)


def baseline_path(root):
    return paths.ledger_dir(root) / "baseline.json"


def _list(root, sub):
    d = paths.ledger_dir(root) / sub
    if not d.is_dir():
        return []
    return [read_json(f) for f in sorted(d.glob("*.json"))]


def write_baseline(root, data):
    """기준선 `ledger/baseline.json` (spec 07). 숫자·정의만 — 원문 없음(P9). bootstrap이 부른다."""
    rec = _stamp(dict(data))
    rec.setdefault("created_at", now_iso())
    paths.ensure(paths.ledger_dir(root))
    write_json(baseline_path(root), rec)
    return rec


def read_baseline(root):
    return read_json(baseline_path(root))
