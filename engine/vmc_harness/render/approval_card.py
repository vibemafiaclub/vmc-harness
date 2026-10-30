"""승인 카드 (spec 08 §승인 카드, 01 P4).

기계 기록 `ledger/approvals/<id>.json`(core.ledger.create_approval이 검증 결과·원복 방법 없으면 이미 거부) → 사람에게 4줄:
  ① 무엇을 결정하나 한 문장  ② 권장과 이유  ③ (위험이 있으면) 위험 한 줄  ④ 원복 되는지
검증 내역·영향 파일·diff·롤백 SQL은 [자세히]에서만. 버튼: 승인 · 거절 · 자세히.
운영 SQL 카드(kind=db_apply 또는 detail.sql)는 드라이런 영향 행 수 + 롤백 SQL이 없으면 카드를 만들지 않는다.
대화창·패널 어디서 눌러도 core.ledger.answer_approval 기록 하나가 처리된다(거절 = finding — 코어).
표시 → 응답 시간은 기록(mark_approval_shown / answered_at), 3분(approval.slow_answer_s) 넘는 유형은 참고 지표.
"""

import hashlib
import json
import statistics

from vmc_harness.core import config, ledger, paths
from vmc_harness.core.clock import parse_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.render import checks

BUTTONS = ["승인", "거절", "자세히"]
CRITERION_LABEL = {1: "운영 반영", 2: "안전 경계", 3: "사람 결정"}
_VOLATILE = ("shown_at", "answered_at", "answer")


class CardError(ValueError):
    """카드를 만들 수 없음(무엇이 빠졌는지만 — 01 P6)."""


def load(root, approval_id):
    rec = read_json(paths.ledger_dir(root) / "approvals" / ("%s.json" % approval_id))
    if rec is None:
        raise KeyError(approval_id)
    return rec


def is_db_apply(rec):
    d = rec.get("detail") or {}
    return rec.get("kind") == "db_apply" or d.get("kind") in ("db_apply", "sql") or bool(d.get("sql"))


def validate(root, rec):
    d = rec.get("detail") or {}
    if not rec.get("verification"):
        raise CardError("승인 기록에 검증 결과 없음")
    if not rec.get("revert_how"):
        raise CardError("승인 기록에 원복 방법 없음")
    if is_db_apply(rec):
        dr = d.get("dryrun") or {}
        if not isinstance(dr.get("affected_rows"), int):
            raise CardError("운영 SQL 카드에 드라이런 영향 행 수 없음")
        if not (d.get("rollback_sql") or d.get("rollback_sql_path")):
            raise CardError("운영 SQL 카드에 롤백 SQL 없음")
    diff = d.get("diff")
    if isinstance(diff, str) and len(diff.splitlines()) > config.get("approval.diff_max_lines", root):
        raise CardError("승인 카드 diff 상한 초과")


def draft_lines(rec):
    """기계 기록 → 4줄 초안(렌더링 엔진이 독자 말로 다듬는다)."""
    d = rec.get("detail") or {}
    label = d.get("label") or ("운영 적용" if is_db_apply(rec) else CRITERION_LABEL.get(rec.get("criterion"), "확인"))
    lines = ["%s · %s" % (label, (rec.get("decision_text") or "").strip())]
    rec_word = "승인" if rec.get("recommend") == "approve" else "거절" if rec.get("recommend") == "reject" else None
    reason = (rec.get("reason") or d.get("reason") or "").strip()
    if rec_word:
        lines.append("권장 %s%s" % (rec_word, (" — " + reason) if reason else ""))
    elif reason:
        lines.append(reason)
    if rec.get("risk"):
        lines.append("위험: %s" % rec["risk"].strip())
    if is_db_apply(rec):
        n = (d.get("dryrun") or {}).get("affected_rows")
        rev = "원복 가능 (롤백 sql 준비됨)" if rec.get("reversible") else "원복 안 됨"
        lines.append("%s · 영향 행 %s" % (rev, n))
    else:
        lines.append(("원복 가능 (%s)" if rec.get("reversible") else "원복 안 됨 (%s)") % rec.get("revert_how"))
    return lines[:4]


def _facts(rec, lines):
    d = rec.get("detail") or {}
    keep = []
    for line in lines:
        for tok in checks.tokens(line):
            if checks._is_name_like(tok):
                keep.append(tok)
    f = {"_keep": sorted(set(keep))}
    if is_db_apply(rec):
        f["affected_rows"] = (d.get("dryrun") or {}).get("affected_rows")
    return f


def _hash(rec):
    stable = {k: v for k, v in rec.items() if k not in _VOLATILE}
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:20]


def card(root, approval, reader="me", allow_llm=True):
    """→ {"approval_id","lines":[…≤4],"text","buttons","criterion","answer","shown_at","checks"} — CardError면 카드 없음."""
    rec = approval if isinstance(approval, dict) else load(root, approval)
    validate(root, rec)
    cpath = paths.cache_dir(root) / "cards" / ("%s.json" % rec["approval_id"])
    h = _hash(rec)
    cached = read_json(cpath, None)
    if cached and cached.get("hash") == h and (cached.get("final") or not allow_llm):
        out = dict(cached["card"])
        out["final"] = bool(cached.get("final"))
    else:
        from vmc_harness.render import engine
        lines = draft_lines(rec)
        draft = "\n".join(lines)
        r = engine.render(root, reader, _facts(rec, lines), draft=draft, kind="approval", allow_llm=allow_llm)
        new_lines = [l for l in r["text"].splitlines() if l.strip()]
        if not (1 <= len(new_lines) <= 4) or not r["checks"]["facts_ok"]:
            new_lines, r = lines, dict(r, text=draft)
        out = {"approval_id": rec["approval_id"], "lines": new_lines, "text": "\n".join(new_lines),
               "buttons": list(BUTTONS), "criterion": rec.get("criterion"), "checks": r["checks"]}
        final = bool(allow_llm or engine._passed(r["checks"]))
        write_json(cpath, {"hash": h, "final": final, "card": out})
        out = dict(out, final=final)
    out["answer"] = rec.get("answer")
    out["shown_at"] = rec.get("shown_at")
    out["task_id"] = rec.get("task_id")
    return out


def detail(root, approval_id):
    """[자세히]: 검증 내역·영향 파일·diff·원복 방법·롤백 SQL·드라이런·기준·시각."""
    rec = load(root, approval_id)
    d = rec.get("detail") or {}
    out = {"approval_id": approval_id, "task_id": rec.get("task_id"),
           "criterion": rec.get("criterion"), "criterion_label": CRITERION_LABEL.get(rec.get("criterion")),
           "verification": rec.get("verification") or [], "files": d.get("files") or [],
           "diff": d.get("diff"), "diff_lines": rec.get("diff_lines"), "revert_how": rec.get("revert_how"),
           "risk": rec.get("risk"), "shown_at": rec.get("shown_at"), "answered_at": rec.get("answered_at"),
           "answer": rec.get("answer")}
    if is_db_apply(rec):
        out["dryrun"] = d.get("dryrun")
        out["rollback_sql"] = d.get("rollback_sql")
        out["rollback_sql_path"] = d.get("rollback_sql_path")
        out["sql_path"] = d.get("sql_path") or d.get("sql")
    return out


def detail_text(root, approval_id):
    x = detail(root, approval_id)
    lines = ["확인 %s · 작업 %s · %s" % (x["approval_id"], x["task_id"], x.get("criterion_label") or "")]
    lines.append("검증:")
    lines += ["  - %s" % v for v in x["verification"]]
    if x["files"]:
        lines.append("영향 파일:")
        lines += ["  - %s" % f for f in x["files"]]
    if x.get("dryrun"):
        lines.append("드라이런: 영향 행 %s" % x["dryrun"].get("affected_rows"))
    if x.get("rollback_sql_path"):
        lines.append("롤백 sql: %s" % x["rollback_sql_path"])
    elif x.get("rollback_sql"):
        lines.append("롤백 sql:\n" + x["rollback_sql"])
    lines.append("원복 방법: %s" % x["revert_how"])
    if x.get("risk"):
        lines.append("위험: %s" % x["risk"])
    if x.get("diff"):
        lines.append("diff (%s줄):\n%s" % (x.get("diff_lines"), x["diff"]))
    return "\n".join(lines)


def mark_shown(root, approval_id):
    return ledger.mark_approval_shown(root, approval_id)


def answer(root, approval_id, ans):
    """승인·거절 — 대화창·패널 공통. 처음 보이는 순간이 기록되지 않았으면 지금을 표시 시각으로 둔다."""
    rec = load(root, approval_id)
    if not rec.get("shown_at"):
        ledger.mark_approval_shown(root, approval_id)
    return ledger.answer_approval(root, approval_id, ans)


def answer_sec(rec):
    a, b = parse_iso(rec.get("shown_at")), parse_iso(rec.get("answered_at"))
    if not a or not b:
        return None
    return max(0.0, (b - a).total_seconds())


def type_of(rec):
    d = rec.get("detail") or {}
    if is_db_apply(rec):
        return "db_apply"
    return d.get("kind") or rec.get("kind") or "criterion_%s" % rec.get("criterion")


def slow_types(root, since=None):
    """유형별 응답 시간 중앙값, 3분(approval.slow_answer_s) 넘는 유형 표시 — 참고 지표(판정에 안 쓴다)."""
    limit = config.get("approval.slow_answer_s", root)
    by = {}
    for rec in ledger.list_approvals(root):
        if not rec or (since and (rec.get("answered_at") or "") < since):
            continue
        s = answer_sec(rec)
        if s is None:
            continue
        by.setdefault(type_of(rec), []).append(s)
    out = []
    for t, xs in sorted(by.items()):
        med = statistics.median(xs)
        out.append({"type": t, "count": len(xs), "median_sec": round(med, 1), "slow": med > limit})
    return out
