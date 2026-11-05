"""자동 머지 등급과 아침 머지 (spec 06).

| 등급 | 대상 | 조건(기계 검사) |
| A | 주석·포맷, 참조 없는 코드·파일 삭제, 현재 동작 고정 테스트 추가 | 동작 동일(Python AST 같음·주석/공백만) 또는 삭제만 + 정적 참조 0, 그리고 빌드 통과 |
| B | 지도를 안 바꾸는 내부 정리(읽기 경로) | 고정 판정 ①~④ 전부 + 보호 영역 밖 + 변경 크기 ≤ loop.max_change_lines + 쓰기 경로 아님 |
| C | 쓰기 경로 | 루프가 실행·요청하지 않는다. 제안만 |
공통: 새 파일·이동 없음(구조 지도 불변 — 테스트 파일 추가만 예외), 보호 영역 밖.

상태: 밤에는 준비만(action `prepared`) → 머지하면 `trial`(merged_at 기록, ledger 판정 대상) → kept/reverted. 다음 날 개발자의 첫 세션 시작 직후 handlers가 `loop merge`를 띄운다.
main이 움직였으면 리베이스(충돌이면 버리고 후보를 대기열로) → 고정 테스트 재실행 → QA 회귀 게이트 → main 빨리감기.
push는 하지 않는다(무인 배포 없음 — 사람 개입 기준 1).
"""

import ast
import datetime as _dt
import re
from pathlib import Path

from vmc_harness.core import config, ledger
from vmc_harness.core.clock import now, now_iso
from vmc_harness.core.gitutil import git
from vmc_harness.loop import candidates, detect, gitops


# ---- 등급 ----

def _name_status(wt, a, b):
    rc, out, _ = git(wt, "diff", "--name-status", "-M", a, b)
    rows = []
    for line in out.splitlines() if rc == 0 else []:
        parts = line.split("\t")
        if len(parts) >= 2:
            rows.append((parts[0][0], parts[1:]))
    return rows


def structure_unchanged(wt, a, b):
    """(ok, 이유) — 새 파일·이름 바꾸기 없음(테스트 파일 추가는 허용)."""
    for st, files in _name_status(wt, a, b):
        if st == "R":
            return False, "파일 이동"
        if st == "A" and not detect.is_test_path(files[0]):
            return False, "새 파일"
    return True, None


def _show(wt, ref, rel):
    rc, out, _ = git(wt, "show", "%s:%s" % (ref, rel))
    return out if rc == 0 else None


def _strip_comments_ws(text):
    s = re.sub(r"//[^\n]*|/\*.*?\*/|#[^\n]*", "", text or "", flags=re.S)
    return re.sub(r"\s+", "", s)


def behavior_identical(wt, a, b, files):
    """주석·포맷만 바뀌었나. Python은 AST 비교, 나머지는 주석·공백 제거 비교."""
    for rel in files:
        old, new = _show(wt, a, rel), _show(wt, b, rel)
        if old is None or new is None:
            return False
        if rel.endswith(".py"):
            try:
                if ast.dump(ast.parse(old)) != ast.dump(ast.parse(new)):
                    return False
            except SyntaxError:
                return False
        elif _strip_comments_ws(old) != _strip_comments_ws(new):
            return False
    return True


def removal_only(wt, a, b):
    """(ok, 지워진 이름들). 코드 파일에 더해진 줄이 공백뿐이고, 지워진 정의 이름 목록."""
    names = set()
    for rel, d in gitops.changed_lines(wt, a, b).items():
        if detect.is_test_path(rel):
            continue
        new_text = (_show(wt, b, rel) or "").splitlines()
        for ln in d["added"]:
            if 0 < ln <= len(new_text) and new_text[ln - 1].strip():
                return False, set()
        old_text = _show(wt, a, rel) or ""
        if d.get("deleted"):
            names.add(Path(rel).stem)
            if rel.endswith(".py"):
                names.update(q.split(".")[-1] for q, _ in detect.py_symbols(old_text))
            continue
        old_lines = old_text.splitlines()
        for ln in d["removed"]:
            if 0 < ln <= len(old_lines):
                m = re.match(r"\s*(?:export\s+)?(?:async\s+)?(?:def|class|function|const|let|var)\s+([A-Za-z_$][\w$]*)",
                             old_lines[ln - 1])
                if m:
                    names.add(m.group(1))
    return True, names


def build_ok(root, wt, a, b, files):
    """빌드 통과 — 사실(bootstrap facts)의 빌드 명령, 없으면 Python 컴파일·tsc. 못 정하면 False."""
    from vmc_harness.loop import fixation
    cmd = fixation._fact_cmd(fixation._facts(root), "build")
    if cmd:
        rc, intr = fixation.run_tests(root, wt, fixation._split(cmd))
        return rc == 0 and not intr
    py = [f for f in files if f.endswith(".py") and (Path(wt) / f).is_file()]
    other = [f for f in files if f.endswith(detect.TS_EXT)]
    ok = True
    if py:
        rc, intr = fixation.run_tests(root, wt, [fixation.project_python(root), "-m", "py_compile"] + py)
        ok = ok and rc == 0 and not intr
    if other:
        tsc = Path(wt) / "node_modules" / ".bin" / ("tsc.cmd" if __import__("sys").platform == "win32" else "tsc")
        if not tsc.exists() or not (Path(wt) / "tsconfig.json").exists():
            return False
        after = _tsc_errors(root, wt, tsc)
        git(wt, "checkout", "-q", a)
        try:
            before = _tsc_errors(root, wt, tsc)
        finally:
            git(wt, "checkout", "-q", "-")
        ok = ok and after is not None and before is not None and after <= before  # 새 오류 0건
    return ok


def _tsc_errors(root, wt, tsc):
    from vmc_harness.loop import procx
    rc, out, _, intr = procx.run([str(tsc), "--noEmit", "-p", str(wt)], cwd=str(wt),
                                 timeout=config.get("loop.test_timeout_s", root), root=root)
    if intr:
        return None
    return {re.sub(r"\(\d+,\d+\)", "", l).strip() for l in out.splitlines() if "error TS" in l}


def grade(root, wt, base, pin_commit, change_commit, cand, change, deadline=None):
    """{"grade": "A"|"B"|None, "reason": str|None, "fixation": dict|None}"""
    files = gitops.changed_files(wt, base, change_commit)
    if not files:
        return {"grade": None, "reason": "바뀐 것 없음", "fixation": None}
    for f in files:
        if candidates.is_protected(root, f):
            return {"grade": None, "reason": "보호 영역", "fixation": None}
    ok, why = structure_unchanged(wt, base, change_commit)
    if not ok:
        return {"grade": None, "reason": why, "fixation": None}
    code_files = [f for f in files if not detect.is_test_path(f)]
    anchor = cand.get("anchor") or {}
    # A — 테스트만 추가
    if not code_files:
        if change != "add_pinning_test":
            return {"grade": None, "reason": "테스트만 바뀜", "fixation": None}
        from vmc_harness.loop import fixation
        argv, _ = fixation.test_invocation(root, wt, files)
        if not argv:
            return {"grade": None, "reason": "테스트 실행 명령 없음", "fixation": None}
        rc, intr = fixation.run_tests(root, wt, argv, deadline=deadline)
        if intr or rc != 0:
            return {"grade": None, "reason": "추가한 테스트 실패", "fixation": None}
        return {"grade": "A", "reason": None, "fixation": None}
    # 쓰기 경로 = C
    for f in code_files:
        sym = anchor.get("symbol") if f == anchor.get("path") else None
        if (Path(wt) / f).exists() and candidates.is_write_path(wt, f, sym):
            return {"grade": "C", "reason": "쓰기 경로", "fixation": None}
    # A — 주석·포맷
    if change in ("comment_format", "internal_cleanup") and behavior_identical(wt, base, change_commit, code_files):
        if build_ok(root, wt, base, change_commit, code_files):
            return {"grade": "A", "reason": None, "fixation": None}
        return {"grade": None, "reason": "빌드 실패", "fixation": None}
    # A — 참조 없는 코드 삭제
    if change == "remove_unreferenced":
        only, names = removal_only(wt, base, change_commit)
        if not only:
            return {"grade": None, "reason": "삭제 외 변경", "fixation": None}
        for n in names:
            if detect.word_refs(wt, n):
                return {"grade": None, "reason": "지운 이름의 참조가 남음: %s" % n, "fixation": None}
        if not build_ok(root, wt, base, change_commit, code_files):
            return {"grade": None, "reason": "빌드 실패", "fixation": None}
        return {"grade": "A", "reason": None, "fixation": None}
    # B — 내부 정리
    if change != "internal_cleanup":
        return {"grade": None, "reason": "직접 처리 대상 아님", "fixation": None}
    size = gitops.diff_size(wt, pin_commit or base, change_commit)
    if size > config.get("loop.max_change_lines", root):
        return {"grade": None, "reason": "변경 크기 상한 초과", "fixation": None}
    if not pin_commit:
        return {"grade": None, "reason": "고정 테스트 없음", "fixation": None}
    from vmc_harness.loop import fixation
    fx = fixation.check(root, wt, base, pin_commit, change_commit, deadline=deadline)
    if fx.get("detail", {}).get("interrupted"):
        return {"grade": None, "reason": "interrupted:%s" % fx["detail"]["interrupted"], "fixation": fx}
    if not fx["ok"]:
        failed = [k for k in ("1", "2", "3", "4") if not fx[k]]
        return {"grade": None, "reason": "고정 판정 실패 %s" % ",".join(failed), "fixation": fx}
    return {"grade": "B", "reason": None, "fixation": fx}


# ---- 아침 머지 ----

def prepared_actions(root):
    return [a for a in ledger.list_actions(root)
            if a.get("status") == "prepared" and (a.get("basis") or {}).get("type") == "cleansing"]


def _discard(root, action, reason):
    ledger.update_action(root, action["action_id"], status="reverted", discarded=True,
                         discard_reason=reason, discarded_at=now_iso())
    cid = (action.get("basis") or {}).get("ref")
    if cid:
        try:
            ledger.update_candidate(root, cid, status="open")
        except Exception:
            pass
    br = action.get("branch")
    if br:
        git(root, "branch", "-D", br)


def morning(root):
    """준비된 A·B 정리를 main에 반영. [{"action_id","result","reason"}]"""
    out = []
    if not config.get("loop.enabled", root) or not config.get("loop.auto_merge", root):
        return out
    from vmc_harness.loop import activity, fixation
    if activity.is_running(root):
        return out
    main = gitops.main_branch(root)
    merged = []
    for a in prepared_actions(root):
        aid = a["action_id"]
        if a.get("grade") not in ("A", "B") and not (a.get("grade") == "S" and config.get("loop.structure_direct", root)):
            continue
        br = a.get("branch")
        if not br or not gitops.rev(root, "refs/heads/%s" % br):
            _discard(root, a, "브랜치 없음")
            out.append({"action_id": aid, "result": "discarded", "reason": "브랜치 없음"})
            continue
        files = gitops.changed_files(root, a.get("base") or main, br)
        if any(candidates.is_protected(root, f) for f in files):
            _discard(root, a, "보호 영역")
            out.append({"action_id": aid, "result": "discarded", "reason": "보호 영역"})
            continue
        try:
            from vmc_harness.qa import results, scenarios
            reg = results.regressions(root, scenarios.covering(root, files))
        except ImportError:
            reg = []
        if reg:
            out.append({"action_id": aid, "result": "blocked", "reason": "QA 회귀"})
            ledger.update_action(root, aid, blocked_by="qa_regression", blocked_at=now_iso())
            continue
        if set(files) & gitops.dirty_files(root):
            out.append({"action_id": aid, "result": "waiting", "reason": "작업 중인 파일"})
            continue
        if not gitops.is_ancestor(root, main, br):
            name = br[len(gitops.BRANCH_PREFIX):]
            wt = gitops.worktrees_dir(root) / name
            if wt.exists():
                gitops.remove_worktree(root, name, delete_branch=False)
            wt.parent.mkdir(parents=True, exist_ok=True)
            rc, _, _ = git(root, "worktree", "add", "-q", str(wt), br, timeout=120)
            if rc != 0:
                out.append({"action_id": aid, "result": "waiting", "reason": "worktree 실패"})
                continue
            ok, why = gitops.rebase_onto(wt, main)
            if ok and a.get("rerun"):  # 루프 티켓(실패 테스트 복구·진단 제안) — 대상 통과·새 실패 0을 다시 잰다
                from vmc_harness.loop import tickets
                ok, why = tickets.morning_recheck(root, wt, a, main)
            elif ok and a.get("pin_tests"):
                argv, _ = fixation.test_invocation(root, wt, a["pin_tests"])
                rc2, intr = fixation.run_tests(root, wt, argv) if argv else (1, None)
                if rc2 != 0 or intr:
                    ok, why = False, "리베이스 뒤 고정 테스트 실패"
            gitops.remove_worktree(root, name, delete_branch=False)
            if not ok:
                _discard(root, a, why)
                out.append({"action_id": aid, "result": "discarded", "reason": why})
                continue
        ok, why = gitops.fast_forward_main(root, main, br)
        if not ok:
            out.append({"action_id": aid, "result": "waiting", "reason": why})
            continue
        commits = _branch_commits(root, a.get("base"), main)
        deadline = (now() + _dt.timedelta(days=config.get("judge.max_days", root))).isoformat(timespec="seconds")
        # 머지 뒤 판정은 ledger 규칙 그대로 — 시험(trial) 상태로 두고 노출(이 영역의 작업)·명시적 부정을 센다
        ledger.update_action(root, aid, status="trial", merged_at=now_iso(), commits=commits or a.get("commits") or [],
                             trial={"exposures": 0, "negatives": 0, "deadline": deadline})
        git(root, "branch", "-D", br)
        cid = (a.get("basis") or {}).get("ref")
        if cid:
            try:
                ledger.update_candidate(root, cid, status="resolved", resolved_reason="merged", resolved_at=now_iso())
            except Exception:
                pass
        merged.append(a)
        out.append({"action_id": aid, "result": "merged", "reason": None})
    if merged:
        _notify(root, merged)
    return out


def _branch_commits(root, base, main):
    if not base:
        return []
    rc, out, _ = git(root, "rev-list", "--reverse", "%s..%s" % (base, main))
    return [l.strip() for l in out.splitlines() if l.strip()] if rc == 0 else []


def _notify(root, merged):
    try:
        from vmc_harness.core import notices
        first = merged[0].get("hypothesis") or ""
        more = " 외 %d건" % (len(merged) - 1) if len(merged) > 1 else ""
        areas = sorted({a.get("area") or "" for a in merged if a.get("area")})
        notices.add(root, "cleansing_merged", "flow",
                    "밤사이 정리 반영: %s%s (자세히: vmc-harness loop status)" % (first, more),
                    area=areas[0] if len(areas) == 1 else None,
                    refs={"action_ids": [a["action_id"] for a in merged]})
    except Exception:
        pass
