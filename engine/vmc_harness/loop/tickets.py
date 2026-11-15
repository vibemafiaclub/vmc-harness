"""루프 티켓 (spec 06 §티켓) — 클렌징 후보 말고 루프가 처리하는 두 종류.

- test_repair: 기준선(부트스트랩 기계 사실 test_baseline)에서 이미 실패하던 테스트. 테스트 파일 하나 = 후보 하나
- ticket: 진단 제안 중 structure(루프가 시도할 수 있는 종류). 사람 [추가] 없이 등록, 패널 [제외]로 취소(사후 원클릭)
  gate·rule·protected는 여기로 오지 않는다 — 근거가 사람 명시 요청이어야 한다(01 P2·P3). 지금처럼 [추가] 대기

처리(클렌징과 같은 틀 — worktree·`claude -p`·중단 신호·밤당 토큰 상한·장부):
  모델이 worktree에서 고친다 → 코드가 숫자로 판정(01 P7): 대상 테스트 통과(복구) + 기준선 대비 새 실패 0
  → 테스트 파일만 바뀌었고 테스트를 약하게 만들지 않았으면 A등급 준비(prepared — 아침 자동 머지, 보고 없음·한 줄 알림)
  → 원본 코드가 바뀌었으면 머지하지 않고 브랜치를 남겨 제안(알림 — 개발자가 그 영역에 오면 한 줄, 01 P4)
  → 안 되면 "시도했으나 안 됨"을 후보에 남기고, loop.repair_max_attempts번 안 되면 보관(archived) — 다시 시도하지 않는다.
    테스트 파일은 지우지 않는다(지우기는 사람 몫)
보호 영역은 대상에서 빼고, 바뀐 파일에 보호 영역이 있으면 버린다.
"""

import ast
import re
from pathlib import Path

from vmc_harness.core import config, ledger
from vmc_harness.core.clock import now_iso
from vmc_harness.core.project import local_dir
from vmc_harness.loop import activity, candidates, cleanse, detect, gitops, procx

KINDS = ("test_repair", "ticket", "predict")
DIAGNOSE_KINDS = ("structure",)  # 루프 티켓으로 자동 등록하는 진단 제안 종류
WEAKEN_RE = re.compile(r"pytest\.mark\.(skip|xfail)|pytest\.(skip|xfail)\(|unittest\.(skip|expectedFailure)|\.skipTest\(")
_PASSED = re.compile(r"^PASSED (\S+)", re.M)
_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)


# ---------------------------------------------------------------- 기준선 → 실패 테스트 복구 후보

def _facts(root):
    try:
        from vmc_harness.bootstrap import facts
        return facts.load(root) or {}
    except Exception:
        return {}


def pytest_runs(root):
    """기준선 실행 중 테스트 id가 있는 것(pytest)."""
    runs = (_facts(root).get("test_baseline") or {}).get("runs") or []
    return [r for r in runs if isinstance(r, dict) and r.get("runner") == "pytest" and r.get("status") in ("pass", "fail")]


def _run_ids(r):
    return list(r.get("failed") or []) + list(r.get("errors") or []) + \
        [c.get("path") if isinstance(c, dict) else c for c in r.get("collection_errors") or []]


def _repo_rel(d, rel):
    d = (d or ".").strip("/")
    return rel if d in ("", ".") else "%s/%s" % (d, rel)


def sync_test_repairs(root):
    """기준선 실패를 테스트 파일별 후보로. 이미 후보가 있는 테스트는 다시 만들지 않는다. 만든 수."""
    have = set()
    for c in ledger.list_candidates(root):
        if c and c.get("kind") == "test_repair":
            have.update((c.get("run_dir") or ".", t) for t in c.get("tests") or [])
    made = 0
    for r in pytest_runs(root):
        d = r.get("dir") or "."
        by_file = {}
        for t in _run_ids(r):
            if t and (d, t) not in have:
                by_file.setdefault(t.split("::")[0], []).append(t)
        for f, ids in sorted(by_file.items()):
            rel = _repo_rel(d, f)
            if candidates.is_protected(root, rel):
                continue
            rec = ledger.create_candidate(root, "test_repair", "실패 테스트 복구 — %s (%d개)" % (rel, len(ids)),
                                          anchor={"path": rel, "symbol": None})
            ledger.update_candidate(root, rec["candidate_id"], run_dir=d, tests=sorted(set(ids)), attempts=[],
                                    area=detect.module_of(root, rel))
            made += 1
    return made


# ---------------------------------------------------------------- 진단 제안 → 티켓

def proposal_key(p):
    return ledger.sha(re.sub(r"\s+", " ", (p.get("title") or "").strip().lower()))[:12]


def register_ticket(root, p, human_requested=False):
    """진단 제안 하나를 루프 티켓으로. 이미 있으면 그것. 장부엔 제목·대상 파일만(근거 원문은 로컬 진단 결과에 — 01 P9)."""
    key = proposal_key(p)
    for c in ledger.list_candidates(root):
        if c and c.get("kind") == "ticket" and c.get("proposal_key") == key:
            return c
    targets = [t.replace("\\", "/") for t in (p.get("targets") or []) if isinstance(t, str) and t.strip()]
    rec = ledger.create_candidate(root, "ticket", p.get("title") or "",
                                  anchor={"path": targets[0], "symbol": None} if targets else None)
    return ledger.update_candidate(root, rec["candidate_id"], proposal_key=key, proposal_kind=p.get("kind"),
                                   targets=targets, blocks=p.get("blocks"), scope=p.get("scope"), attempts=[],
                                   human_requested=bool(human_requested), source="diagnose")


def sync_diagnosis(root, d=None):
    """진단 결과의 structure 제안을 티켓으로(사람 [추가] 없이). 이미 답한 제안([추가]·[제외])은 건드리지 않는다. 등록 수."""
    from vmc_harness.bootstrap import diagnose
    from vmc_harness.core.jsonio import write_json
    d = d if d is not None else (diagnose.load(root) or {})
    made, changed = 0, False
    for p in d.get("proposals") or []:
        if not isinstance(p, dict) or p.get("kind") not in DIAGNOSE_KINDS or p.get("excluded") or p.get("ticket_id"):
            continue
        if p.get("accepted") and not p.get("ticket_id"):
            continue  # 옛 [추가] 기록(티켓 이전) — 그대로 둔다
        c = register_ticket(root, p)
        p["ticket_id"] = c["candidate_id"]
        p["auto"] = "loop_ticket"
        made += 1
        changed = True
    if changed:
        write_json(diagnose.diagnosis_path(root), d)
    return made


def cancel_ticket(root, candidate_id, reason="패널 [제외]"):
    """[제외] — 티켓 취소. 준비된 브랜치·조치도 버리고, 이미 반영됐으면 되돌린다. 갱신된 후보."""
    c = ledger.get_candidate(root, candidate_id)
    if not c:
        return None
    aid = c.get("action_id")
    if aid:
        a = ledger.get_action(root, aid) or {}
        if a.get("status") == "prepared":
            ledger.update_action(root, aid, status="reverted", discarded=True, discard_reason=reason,
                                 discarded_at=now_iso())
        elif a.get("status") in ("trial", "extended", "kept"):
            try:
                from vmc_harness.ledger import revert
                revert.revert_action(root, aid, reason, by_human=True, make_finding=False)
            except Exception:
                pass
    if c.get("branch"):
        from vmc_harness.core.gitutil import git
        git(root, "branch", "-D", c["branch"])
    return ledger.update_candidate(root, candidate_id, status="excluded", resolved_reason="excluded",
                                   resolved_at=now_iso())


def _proposal_detail(root, cand):
    """근거 원문은 장부가 아니라 로컬 진단 결과에서 꺼낸다."""
    try:
        from vmc_harness.bootstrap import diagnose
        for p in (diagnose.load(root) or {}).get("proposals") or []:
            if isinstance(p, dict) and (p.get("ticket_id") == cand["candidate_id"] or proposal_key(p) == cand.get("proposal_key")):
                return p
    except Exception:
        pass
    return {}


def sync(root):
    """밤마다 처리 전에(LLM 없음). {"test_repair": n, "ticket": n}"""
    out = {"test_repair": 0, "ticket": 0}
    out["test_repair"] = sync_test_repairs(root)
    try:
        out["ticket"] = sync_diagnosis(root)
    except Exception:
        pass
    return out


# ---------------------------------------------------------------- 테스트 실행(pytest — 기준선과 같은 방식)

def run_pytest(root, wt, d, ids=None, deadline=None, tb="no"):
    """{"ran": bool, "missing": bool, "passed": set, "failed": set, "interrupted", "output"}.
    파이썬은 원래 프로젝트의 가상환경(worktree엔 없다)."""
    import os
    from vmc_harness.bootstrap import facts
    py = facts.find_python(root, d)[0]
    argv = [py, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-rA", "--tb=%s" % tb, "--no-header",
            "--continue-on-collection-errors"] + list(ids or [])
    env = dict(os.environ)
    env["VMC_HARNESS_NESTED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # worktree에 __pycache__를 남기지 않는다(변경 파일로 잡히고, 같은 초 안의 수정은 낡은 바이트코드를 읽는다)
    cwd = Path(wt) / d if d not in (None, "", ".") else Path(wt)
    rc, out, err, intr = procx.run(argv, cwd=str(cwd), timeout=config.get("loop.test_timeout_s", root), env=env,
                                   root=root, deadline=deadline)
    text = (out or "") + "\n" + (err or "")
    return {"ran": intr is None and rc in (0, 1), "missing": rc == 4, "interrupted": intr, "rc": rc,
            "passed": set(_PASSED.findall(text)), "failed": set(_FAILED.findall(text)), "output": text}


def target_passed(t, res):
    """대상 id가 통과했나 — 파일 단위(수집 오류) 대상은 그 파일의 테스트가 하나 이상 통과하고 실패가 없을 때."""
    if t in res["passed"]:
        return True
    if "::" in t:
        return False
    pre = t + "::"
    return any(p.startswith(pre) for p in res["passed"]) and not any(f == t or f.startswith(pre) for f in res["failed"])


def _baseline_ids(r):
    return set(_run_ids(r))


def _in_baseline(fid, base):
    return fid in base or fid.split("::")[0] in base


def new_failures(root, wt, base_commit, deadline=None):
    """기준선 대비 새 실패 {dir: [id]} — 기준선 밖 실패가 보이면 바꾸기 전 커밋에서도 돌려 그때도 실패한 건 뺀다
    (기준선 뒤 사람 커밋으로 생긴 실패를 루프 탓으로 세지 않게). None = 잴 수 없음, "interrupted:<사유>" = 중단."""
    from vmc_harness.core.gitutil import git
    out = {}
    runs = pytest_runs(root)
    if not runs:
        return None
    for r in runs:
        d = r.get("dir") or "."
        after = run_pytest(root, wt, d, deadline=deadline)
        if after["interrupted"]:
            return "interrupted:%s" % after["interrupted"]
        if not after["ran"]:
            return None
        base = _baseline_ids(r)
        new = {f for f in after["failed"] if not _in_baseline(f, base)}
        if new:
            git(wt, "checkout", "-q", base_commit)
            try:
                before = run_pytest(root, wt, d, deadline=deadline)
            finally:
                git(wt, "checkout", "-q", "-")
            if before["interrupted"]:
                return "interrupted:%s" % before["interrupted"]
            if before["ran"]:
                new -= before["failed"]
        if new:
            out[d] = sorted(new)
    return out


# ---------------------------------------------------------------- 테스트를 약하게 만들었나(기계 검사)

def _assert_count(text):
    try:
        tree = ast.parse(text or "")
    except SyntaxError:
        return None
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            n += 1
        elif isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            if name.startswith("assert") or name == "raises":
                n += 1
    return n


def weakened(wt, base, head, files):
    """테스트 단언을 줄였거나 skip·xfail을 새로 붙였으면 그 이유(자동 머지 대신 제안으로). 아니면 None."""
    from vmc_harness.core.gitutil import git
    for rel in files:
        if not rel.endswith(".py"):
            continue
        rc, old, _ = git(wt, "show", "%s:%s" % (base, rel))
        if rc != 0:
            continue  # 새 파일
        rc2, new, _ = git(wt, "show", "%s:%s" % (head, rel))
        if rc2 != 0:
            return "테스트 파일 삭제: %s" % rel
        before, after = _assert_count(old), _assert_count(new)
        if before is not None and (after is None or after < before):
            return "단언 수 감소: %s (%d→%s)" % (rel, before, after)
        old_marks = len(WEAKEN_RE.findall(old))
        if len(WEAKEN_RE.findall(new)) > old_marks:
            return "skip·xfail 추가: %s" % rel
    return None


# ---------------------------------------------------------------- 처리

REPAIR_PROMPT = """목표: 아래 테스트가 통과하게 한다. 기준선(하네스 설치 때)부터 실패하던 테스트다.
대상({d} 폴더에서 pytest로 돈다): {tests}

참고: 지금 실패 출력 {out_path}, 코드 전체(Read·Glob·Grep)
제약:
- 테스트가 확인하는 동작을 지우거나 약하게 만들지 않는다(단언 삭제·skip·xfail·테스트 삭제 없이 통과시킨다)
- 테스트 파일만 고쳐서 될 일이면 테스트만 고친다. 원본 코드를 고쳐야 하면 고쳐도 되지만 그 변경은 바로 반영되지 않고 개발자에게 제안으로 남는다
- 보호 영역은 고치지 않는다: {protected}
- 외부 서비스·DB·네트워크에 닿지 않는다
출력 형식: JSON 객체만. {{"done": true|false, "source_change": true|false, "note": "한 줄"}}
"""

TICKET_PROMPT = """목표: {title}
막으려는 것: {blocks}
근거: {evidence}
범위: {scope}

참고: 코드 전체(Read·Glob·Grep), 진단이 바꾸게 될 파일로 적은 것: {targets}
제약:
- 이 제안 범위 안에서만 바꾼다
- 기존 테스트가 새로 실패하면 버려진다. 테스트를 지우거나 약하게 만들지 않는다
- 보호 영역은 고치지 않는다: {protected}
- 테스트만 추가·고치는 변경은 검증 뒤 반영되고, 원본 코드 변경은 바로 반영되지 않고 개발자에게 제안으로 남는다
- 외부 서비스·DB·네트워크에 닿지 않는다
출력 형식: JSON 객체만. {{"done": true|false, "note": "한 줄"}}
"""


def _not_counted(reason):
    return candidates.not_counted(reason)


def _failed(root, cand, reason, tokens):
    """시도했으나 안 됨 — 기록하고, 상한이면 보관(다시 시도 안 함 + 알림)."""
    c = ledger.get_candidate(root, cand["candidate_id"]) or cand
    atts = list(c.get("attempts") or [])
    if not _not_counted(reason):
        atts.append({"at": now_iso(), "reason": str(reason)[:200], "tokens": int(tokens or 0)})
    fields = {"attempts": atts, "last_failure": str(reason)[:200]}
    archived = len(atts) >= config.get("loop.repair_max_attempts", root)
    if archived:
        fields.update(status="archived", archived_at=now_iso())
    ledger.update_candidate(root, cand["candidate_id"], **fields)
    if archived:
        from vmc_harness.core import notices
        a = cand.get("anchor") or {}
        if cand["kind"] == "test_repair":
            text = "실패 테스트 복구를 %d번 시도했지만 안 됨 — 보관: %s (%d개, 마지막: %s). 테스트 파일은 그대로 두었고, 지울지는 개발자가 정한다" % (
                len(atts), a.get("path"), len(cand.get("tests") or []), str(reason)[:80])
        else:
            text = "진단 제안 '%s'을(를) %d번 시도했지만 안 됨 — 보관(마지막: %s)" % (cand.get("summary"), len(atts), str(reason)[:80])
        notices.add(root, "loop_ticket_archived", "flow", text, area=a.get("path"),
                    refs={"candidate_id": cand["candidate_id"], "attempts": len(atts)})
    return {"outcome": "archived" if archived else "failed", "reason": reason, "tokens": tokens}


PREDICT_PROMPT = """목표: 이 영역에서 작업이 예측보다 오래 걸린 원인을 레포 구조로 없앤다.
기계가 정한 원인: {cause} — 같은 영역 작업 {n}건에서 이 단계가 비슷한 과거 작업보다 중앙값 {excess}초 더 걸렸다.
영역: {area}
그 작업들이 하려던 것: {intents}
그 작업들이 바꾼 파일: {targets}
고르는 순서: 구조(모듈 경계·파일 배치·중복 통일·느린 검증 나누기) → 게이트(검사·테스트). 지침 문서를 늘리는 것은 마지막이다.
제약:
- 동작은 바꾸지 않는다. 기존 테스트가 새로 실패하면 버려진다. 테스트를 지우거나 약하게 만들지 않는다
- 보호 영역은 고치지 않는다: {protected}
- DB 쓰기·결재·감사 같은 쓰기 경로는 고치지 않는다
- 외부 서비스·DB·네트워크에 닿지 않는다
- 고칠 것이 없다고 판단하면 아무것도 바꾸지 않는다
출력 형식: JSON 객체만. {{"done": true|false, "note": "한 줄 — 무엇을 바꿨고 앞 순위로 못 푼 이유"}}
"""


def repo_checks(root, wt, base, head, files):
    """구조 직접 합치기(loop.structure_direct, spec 13)의 통과 기준 — 사람 커밋과 같은 게이트.
    레포 커밋 훅(pre-commit, 없으면 통과) + 빌드(.harness/config.json build_cmd가 있으면 그것, 없으면 merge.build_ok). (ok, 이유)."""
    import json
    from vmc_harness.loop import fixation, merge
    rc, intr = fixation.run_tests(root, wt, ["git", "hook", "run", "--ignore-missing", "pre-commit"])
    if rc != 0 or intr:
        return False, "커밋 훅 실패"
    try:
        cmd = (json.loads((Path(root) / ".harness" / "config.json").read_text(encoding="utf-8")) or {}).get("build_cmd")
    except (OSError, ValueError):
        cmd = None
    if cmd:
        rc, intr = fixation.run_tests(root, wt, list(cmd) if isinstance(cmd, list) else fixation._split(cmd))
        return (rc == 0 and not intr), (None if rc == 0 and not intr else "빌드 실패")
    ok = merge.build_ok(root, wt, base, head, files)
    return ok, (None if ok else "빌드 실패")


def _record_direct(root, cand, branch, base, head, changed, wt):
    """구조 직접 합치기 준비(S등급). 아침 머지가 리베이스 뒤 repo_checks를 다시 돌리고 합친다."""
    from vmc_harness.loop import merge
    layer = "code" if merge.structure_unchanged(wt, base, head)[0] else "structure"
    area = changed[0]
    extra = {"grade": "S", "branch": branch, "base": base, "candidate_id": cand["candidate_id"],
             "change": "structure_direct", "loop_kind": cand["kind"], "pin_tests": [], "rerun": {"repo_checks": True},
             "area": area, "files": list(changed)[:50], "module": detect.module_of(root, area), "prepared_at": now_iso()}
    if cand["kind"] == "predict":
        key = cand.get("predict_key") or "%s|%s" % (cand.get("area") or "-", cand.get("cause"))
        basis = {"type": "predict_miss", "ref": cand["candidate_id"], "key": key}
        extra["predict"] = {"key": key, "cause": cand.get("cause"), "phase": cand.get("phase") or cand.get("cause"),
                            "area": cand.get("area"), "baseline_excess": cand.get("baseline_excess")}
        hyp = "%s — 이 영역 %s 초과가 줄어든다" % (cand.get("summary"), cand.get("phase") or cand.get("cause"))
        target = {"predict": key}
    elif cand.get("auto_apply") and cand.get("auto_basis"):
        basis = dict(cand["auto_basis"])
        hyp = "진단 제안 자동 반영 — %s" % cand.get("summary")
        target = {"area": area}
        extra["auto"] = dict(cand.get("auto_snapshot") or {})
    else:
        basis = {"type": "cleansing", "ref": cand["candidate_id"]}
        hyp = "진단 제안(구조 직접) — %s" % cand.get("summary")
        target = {"area": area}
    return ledger.create_action(root, basis, layer, hyp, target=target, status="prepared", extra=extra)


def _propose(root, cand, branch, reason, verified):
    """원본 코드 변경 — 머지하지 않고 브랜치를 남겨 제안(01 P4 — 승인 요청이 아니라 알림)."""
    from vmc_harness.core import notices
    a = cand.get("anchor") or {}
    what = {"test_repair": "실패 테스트 복구", "predict": "예측 회고"}.get(cand["kind"], "진단 제안")
    notices.add(root, "loop_ticket_proposal", "flow",
                "%s 준비됨(반영 안 함 — %s): %s · 브랜치 %s · %s" % (what, reason, cand.get("summary"), branch, verified),
                area=a.get("path"), refs={"candidate_id": cand["candidate_id"], "branch": branch, "reason": reason})
    ledger.update_candidate(root, cand["candidate_id"], status="proposed", proposed_at=now_iso(),
                            proposed_reason=reason, branch=branch, verified=verified)
    return {"outcome": "proposed", "reason": reason}


def _touches_protected(root, files):
    return [f for f in files if candidates.is_protected(root, f)]


def _deleted_tests(wt, base, head):
    from vmc_harness.core.gitutil import git
    rc, out, _ = git(wt, "diff", "--name-status", base, head)
    return [l.split("\t", 1)[1] for l in out.splitlines() if rc == 0 and l.startswith("D\t")
            and detect.is_test_path(l.split("\t", 1)[1])]


def process(root, cand, deadline=None):
    """티켓 1건. {"outcome": prepared|proposed|resolved|expired|skipped|failed|archived|yielded, "reason", "tokens", ...}"""
    cid = cand["candidate_id"]
    kind = cand.get("kind")
    anchor = cand.get("anchor") or {}
    area_files = ([anchor["path"]] if kind == "test_repair" else list(cand.get("targets") or [])) if anchor.get("path") else []
    main = gitops.main_branch(root)
    main_sha = gitops.rev(root, main)
    if set(area_files) & candidates.busy_files(root, main):
        return {"outcome": "skipped", "reason": "작업 중인 영역", "tokens": 0}
    if _touches_protected(root, area_files):
        ledger.update_candidate(root, cid, status="expired", resolved_reason="protected", resolved_at=now_iso())
        return {"outcome": "expired", "reason": "보호 영역", "tokens": 0}
    if anchor.get("path") and cleanse.area_busy(root, anchor["path"]):
        return {"outcome": "skipped", "reason": "같은 영역 조치 시험 중", "tokens": 0}
    name = cid.lower()
    wt, branch = gitops.add_worktree(root, name, main_sha)
    if wt is None:
        return {"outcome": "failed", "reason": "worktree: %s" % branch, "tokens": 0}
    keep_branch, tokens = False, 0
    protected = ", ".join(candidates.protected_globs(root)) or "없음"
    try:
        cleanse._link_node_modules(root, wt)
        if kind == "test_repair":
            d = cand.get("run_dir") or "."
            r0 = run_pytest(root, wt, d, ids=cand.get("tests"), deadline=deadline, tb="short")
            if r0["interrupted"]:
                return {"outcome": "yielded", "reason": r0["interrupted"], "tokens": 0}
            if r0["missing"]:
                ledger.update_candidate(root, cid, status="resolved", resolved_reason="missing", resolved_at=now_iso())
                return {"outcome": "resolved", "reason": "대상 테스트가 없어짐", "tokens": 0}
            if not r0["ran"]:
                return {"outcome": "skipped", "reason": "테스트 실행 불가(rc %s)" % r0.get("rc"), "tokens": 0}
            still = [t for t in cand.get("tests") or [] if not target_passed(t, r0)]
            if not still:
                ledger.update_candidate(root, cid, status="resolved", resolved_reason="natural", resolved_at=now_iso())
                return {"outcome": "resolved", "reason": "자연 해소", "tokens": 0}
            out_path = local_dir(root) / "loop-repair" / ("%s.txt" % cid)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            tail = config.get("loop.repair_output_tail_lines", root)
            out_path.write_text("\n".join(r0["output"].splitlines()[-tail:]), encoding="utf-8")
            prompt = REPAIR_PROMPT.format(d=d, tests=", ".join(still), out_path=str(out_path), protected=protected)
            add_dirs = [out_path.parent]
        elif kind == "predict":
            from vmc_harness.task.analysis import LABEL
            prompt = PREDICT_PROMPT.format(cause=LABEL.get(cand.get("cause"), cand.get("cause")),
                                           n=len(cand.get("task_ids") or []), excess=cand.get("baseline_excess"),
                                           area=cand.get("area") or "루트", intents=" / ".join(cand.get("intents") or []) or "-",
                                           targets=", ".join(cand.get("targets") or []) or "적지 않음", protected=protected)
            still, add_dirs = [], None
        else:
            p = _proposal_detail(root, cand)
            prompt = TICKET_PROMPT.format(title=cand.get("summary"), blocks=cand.get("blocks") or p.get("blocks") or "-",
                                          evidence=p.get("evidence") or "-", scope=cand.get("scope") or p.get("scope") or "-",
                                          targets=", ".join(cand.get("targets") or []) or "적지 않음", protected=protected)
            still, add_dirs = [], None
        r = cleanse.claude(root, prompt, wt, cleanse.EDIT_TOOLS, deadline=deadline, add_dirs=add_dirs)
        tokens += int(r.get("tokens") or 0)
        if r.get("interrupted"):
            return {"outcome": "yielded", "reason": r["interrupted"], "tokens": tokens}
        if not r.get("ok"):
            return _failed(root, cand, "모델 실행 실패%s" % (": " + r["error"] if r.get("error") else ""), tokens)
        files = cleanse._status_files(wt)
        if not files:
            return _failed(root, cand, "바뀐 것 없음", tokens)
        hit = _touches_protected(root, files)
        if hit:
            return _failed(root, cand, "보호 영역을 고침: %s" % ", ".join(hit[:3]), tokens)
        head = gitops.commit_all(wt, ("test(vmc-harness): 실패 테스트 복구 — %s" % anchor.get("path")) if kind == "test_repair"
                                 else "chore(vmc-harness): %s" % cand.get("summary"))
        if not head:
            return _failed(root, cand, "바뀐 것 없음", tokens)
        dels = _deleted_tests(wt, main_sha, head)
        if dels:
            return _failed(root, cand, "테스트 파일 삭제: %s" % ", ".join(dels[:3]), tokens)
        if activity.stop_requested(root):
            return {"outcome": "yielded", "reason": "stop", "tokens": tokens}
        # 숫자로 판정(01 P7): 대상 통과 + 새 실패 0
        if kind == "test_repair":
            rt = run_pytest(root, wt, cand.get("run_dir") or ".", ids=still, deadline=deadline)
            if rt["interrupted"]:
                return {"outcome": "yielded", "reason": rt["interrupted"], "tokens": tokens}
            fixed = [t for t in still if rt["ran"] and target_passed(t, rt)]
            if len(fixed) < len(still):
                return _failed(root, cand, "대상 테스트 %d/%d개만 통과" % (len(fixed), len(still)), tokens)
        nf = new_failures(root, wt, main_sha, deadline=deadline)
        if isinstance(nf, str):
            return {"outcome": "yielded", "reason": nf.split(":", 1)[1], "tokens": tokens}
        if nf:
            n = sum(len(v) for v in nf.values())
            return _failed(root, cand, "새 실패 %d개" % n, tokens)
        verified = ("대상 %d개 통과 · 새 실패 0" % len(still)) if kind == "test_repair" else \
            ("새 실패 0" if nf is not None else "테스트 실행 못 함(미검증)")
        # main이 움직였으면 리베이스(충돌이면 대기열로)
        cur = gitops.rev(root, main)
        if cur != main_sha:
            ok, _ = gitops.rebase_onto(wt, cur)
            if not ok:
                return {"outcome": "discarded", "reason": "main 충돌", "tokens": tokens}
            main_sha = cur
        head = gitops.rev(wt, "HEAD")
        changed = gitops.changed_files(wt, main_sha, head)
        from vmc_harness.loop import merge
        tests_only = all(detect.is_test_path(f) for f in changed) and merge.structure_unchanged(wt, main_sha, head)[0]
        weak = weakened(wt, main_sha, head, [f for f in changed if detect.is_test_path(f)])
        auto = bool(cand.get("auto_apply") and cand.get("auto_basis"))  # 진단 자동 반영(bootstrap.autoapply 검사 통과)
        direct = auto or (not tests_only and config.get("loop.structure_direct", root))
        if direct and not weak and not any(candidates.is_write_path(root, f) for f in changed):
            if auto:  # 조이기만 — 삭제·훅 줄이기·하네스 상태·사람 지침 변경·변경 크기 초과면 반영하지 않고 제안으로
                from vmc_harness.bootstrap import autoapply
                bad = autoapply.diff_problems(root, wt, main_sha, head, changed)
                if bad:
                    keep_branch = True
                    return dict(_propose(root, cand, branch, "자동 반영 검사 미통과: " + "; ".join(bad[:3]), verified),
                                tokens=tokens)
            ok, why = repo_checks(root, wt, main_sha, head, changed)
            if not ok:
                return _failed(root, cand, why, tokens)
            try:
                action = _record_direct(root, cand, branch, main_sha, head, changed, wt)
            except ledger.MetaGateError as e:
                return {"outcome": "skipped", "reason": str(e), "tokens": tokens}
            commits = cleanse._rewrite_trailers(wt, branch, main_sha, action["action_id"])
            ledger.update_action(root, action["action_id"], commits=commits)
            ledger.update_candidate(root, cid, status="prepared", action_id=action["action_id"], branch=branch,
                                    verified="커밋 훅·빌드 통과")
            keep_branch = True
            return {"outcome": "prepared", "reason": None, "tokens": tokens, "action_id": action["action_id"], "grade": "S"}
        if not tests_only or weak or nf is None:
            keep_branch = True
            why = weak or ("원본 코드 수정 필요" if not tests_only else "검증 못 함")
            return dict(_propose(root, cand, branch, why, verified), tokens=tokens)
        try:
            action = _record_action(root, cand, branch, main_sha, changed, still)
        except ledger.MetaGateError as e:
            return {"outcome": "skipped", "reason": str(e), "tokens": tokens}
        commits = cleanse._rewrite_trailers(wt, branch, main_sha, action["action_id"])
        ledger.update_action(root, action["action_id"], commits=commits)
        ledger.update_candidate(root, cid, status="prepared", action_id=action["action_id"], branch=branch,
                                verified=verified)
        keep_branch = True
        return {"outcome": "prepared", "reason": None, "tokens": tokens, "action_id": action["action_id"], "grade": "A"}
    finally:
        gitops.remove_worktree(root, name, delete_branch=not keep_branch)


def _record_action(root, cand, branch, base, changed, still):
    """A등급 준비(테스트 파일만, 대상 통과·새 실패 0). 아침 머지(merge.morning)가 그대로 처리 — 기록은 클렌징과 같은 틀."""
    a = cand.get("anchor") or {}
    area = a.get("path") if cand["kind"] == "test_repair" and a.get("path") in changed else changed[0]
    if cand["kind"] == "test_repair":
        hyp = "실패 테스트 복구(테스트만) — %s %d개" % (a.get("path"), len(still))
        rerun = {"dir": cand.get("run_dir") or ".", "ids": list(still)}
    else:
        hyp = "진단 제안(테스트만) — %s" % cand.get("summary")
        rerun = {"dir": None, "ids": []}  # 아침엔 새 실패 0을 다시 잰다
    extra = {"grade": "A", "branch": branch, "base": base, "candidate_id": cand["candidate_id"],
             "change": "repair_test" if cand["kind"] == "test_repair" else "ticket_tests", "loop_kind": cand["kind"],
             "pin_tests": [], "rerun": rerun, "area": area, "anchor": a, "module": detect.module_of(root, area),
             "prepared_at": now_iso()}
    return ledger.create_action(root, {"type": "cleansing", "ref": cand["candidate_id"]}, "code", hyp,
                                target={"area": area}, status="prepared", extra=extra)


def morning_recheck(root, wt, action, base):
    """아침 머지 직전(리베이스 뒤) 다시 판정. (ok, 이유)."""
    rr = action.get("rerun") or {}
    if rr.get("repo_checks"):  # 구조 직접 합치기(S) — 리베이스 뒤 커밋 훅·빌드를 다시
        head = gitops.rev(wt, "HEAD")
        return repo_checks(root, wt, base, head, gitops.changed_files(wt, base, head))
    if rr.get("ids"):
        res = run_pytest(root, wt, rr.get("dir") or ".", ids=rr["ids"])
        if not res["ran"] or not all(target_passed(t, res) for t in rr["ids"]):
            return False, "리베이스 뒤 대상 테스트 실패"
    nf = new_failures(root, wt, base)
    if isinstance(nf, str) or nf is None:
        return False, "리베이스 뒤 테스트 판정 못 함"
    if nf:
        return False, "리베이스 뒤 새 실패"
    return True, None


def counts(root):
    out = {}
    for c in ledger.list_candidates(root):
        if c and c.get("kind") in KINDS:
            k = "%s:%s" % (c["kind"], c.get("status"))
            out[k] = out.get(k, 0) + 1
    return out
