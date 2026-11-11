"""클렌징 실행 단위 (spec 06) — 별도 git worktree에서 `claude -p` headless(개발자 구독)로 정리를 준비한다.

모델에게는 목표·참고·제약·출력 형식만 준다(01 P6). 순서·검증은 코드가 한다:
- 후보 발견(discover): 검출 도구 결과를 참고 파일로 주고 모델이 자유롭게 후보를 낸다 → 재확인 조건이 참인 것만 장부에
- 처리(process): 낡음 확인 → 권한(직접/제안) → 안전망 → [고정 테스트 커밋] → [정리 커밋] → 등급 → prepared
중단 신호(사람 복귀)·마감·밤당 토큰 상한이 걸리면 하던 단위를 버리고 후보는 대기열(open)로 남긴다.
"""

import json
import os
import re
import shutil
import sys
from pathlib import Path

from vmc_harness.core import config, ledger
from vmc_harness.core.clock import now_iso
from vmc_harness.core.jsonio import write_json
from vmc_harness.core.project import local_dir
from vmc_harness.loop import activity, candidates, detect, gitops, procx

EDIT_TOOLS = "Read,Edit,Write,Glob,Grep"
READ_TOOLS = "Read,Glob,Grep"


# ---- claude -p ----

def claude(root, prompt, cwd, tools, deadline=None, add_dirs=None):
    """{"text","tokens","ok","interrupted"} — 실패해도 dict(ok=False). 훅 재귀 방지: --setting-sources "" + NESTED=1."""
    exe = shutil.which("claude")
    if not exe:
        return {"text": "", "tokens": 0, "ok": False, "interrupted": None, "error": "claude 없음"}
    model = os.environ.get("VMC_HARNESS_EVAL_MODEL") or config.get("loop.model", root)
    args = [exe, "-p", "--model", model, "--output-format", "json", "--setting-sources", "",
            "--strict-mcp-config", "--no-session-persistence", "--tools", tools,
            "--permission-mode", "acceptEdits"]
    for d in add_dirs or []:
        args += ["--add-dir", str(d)]
    env = dict(os.environ)
    env["VMC_HARNESS_NESTED"] = "1"
    rc, out, err, intr = procx.run(args, cwd=str(cwd), timeout=config.get("loop.unit_timeout_s", root),
                                   input_text=prompt, env=env, root=root, deadline=deadline)
    res = {"text": "", "tokens": 0, "ok": False, "interrupted": intr}
    try:
        data = json.loads(out) if out.strip() else {}
    except ValueError:
        data = {}
    usage = data.get("usage") or {}
    res["tokens"] = sum(int(usage.get(k) or 0) for k in
                        ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    res["text"] = (data.get("result") or "").strip()
    res["ok"] = rc == 0 and not data.get("is_error") and not intr
    if not res["ok"] and not intr:
        res["error"] = (err or out or "").strip()[:300]
    return res


def _json_in(text, kind="array"):
    """모델 출력에서 JSON 배열/객체 하나를 꺼낸다."""
    m = re.search(r"```(?:json)?\s*(.*?)```", text or "", re.S)
    body = m.group(1) if m else (text or "")
    open_ch, close_ch = ("[", "]") if kind == "array" else ("{", "}")
    i, j = body.find(open_ch), body.rfind(close_ch)
    if i < 0 or j < i:
        return None
    try:
        return json.loads(body[i:j + 1])
    except ValueError:
        return None


# ---- 후보 발견 ----

DISCOVER_PROMPT = """목표: 이 코드베이스에서 동작은 그대로 두고 정리할 곳(클렌징 후보)을 찾는다. 에이전트가 실제로 헤맨 영역(참고 파일의 wander)이 먼저다. 결과와 무관한 정리에 시간을 쓰지 않는다.

참고할 수 있는 것:
- {detect_path} — 검출 도구 결과(참조 0 코드·파일, 중복 함수, 거대 파일·함수, 헤맨 파일). 쓸지는 판단
- 코드 전체(Read·Glob·Grep)

제약:
- 보호 영역은 대상이 아니다: {protected}
- 낡은 문서는 대상이 아니다
- 후보마다 기계로 다시 확인할 조건을 한 줄로 붙인다. 지금 거짓이면 버려진다
- 앵커는 경로+심볼 이름(줄 번호 아님)
- 이미 있는 후보는 다시 내지 않는다: {existing}

출력 형식: JSON 배열만. 항목 = {{"summary": "한 줄", "change": "<종류>", "recheck": {{"type": "grep|ast", "query": "<한 줄>", "path": "<grep 대상 glob(선택)>", "expect": "match|none"}}, "anchor": {{"path": "<경로>", "symbol": "<심볼|null>"}}}}
change 종류: internal_cleanup(함수 안·같은 모듈 안 정리) | remove_unreferenced(참조 0 제거) | add_pinning_test(현재 동작 고정 테스트 추가) | comment_format | add_module | move | split | interface_change | cross_module_dedupe | write_path
ast 질의: "unreferenced <path>::<symbol>" | "unreferenced_file <path>" | "dup <path>::<symbol> <path>::<symbol>" | "long_function <path>::<symbol> > <n>" | "long_file <path> > <n>" | "defined <path>::<symbol>"
"""


def detection_report(root):
    return {"unreferenced": detect.unreferenced(root), "duplicates": detect.duplicates(root),
            "giant": detect.giant(root), "wander": detect.wander(root)}


def discover(root, wt, deadline=None):
    """(받은 후보 수, 토큰, interrupted)."""
    rep = detection_report(wt)
    rep["wander"] = detect.wander(root)  # 세션 기록은 원래 프로젝트 경로 기준
    tmp = local_dir(root) / "loop-detect.json"
    write_json(tmp, rep)
    existing = sorted({"%s::%s" % ((c.get("anchor") or {}).get("path"), (c.get("anchor") or {}).get("symbol"))
                       for c in ledger.list_candidates(root)
                       if c and c.get("kind") == "cleansing" and c.get("status") in candidates.DEDUPE_STATUSES})
    prompt = DISCOVER_PROMPT.format(detect_path=str(tmp), protected=", ".join(candidates.protected_globs(root)) or "없음",
                                    existing=", ".join(existing) or "없음")
    r = claude(root, prompt, wt, READ_TOOLS, deadline=deadline, add_dirs=[tmp.parent])
    accepted = 0
    if r["ok"]:
        for prop in _json_in(r["text"], "array") or []:
            rec, _ = candidates.accept(root, prop, check_root=wt)
            if rec:
                accepted += 1
    return accepted, r["tokens"], r.get("interrupted")


# ---- 처리 ----

PIN_PROMPT = """목표: {anchor}의 현재 동작을 고정하는 테스트를 쓴다. 이 테스트는 다음 단계의 정리가 동작을 바꾸면 실패해야 한다(반환값·분기·예외까지 구체적으로 확인).

참고: 기존 테스트의 위치·관례(Glob·Grep), 대상 코드. 테스트 실행: {runner}
제약:
- 테스트 파일만 만들거나 고친다. 제품 코드는 고치지 않는다
- 지금 코드에서 통과해야 한다(기대값은 지금 동작 그대로)
- 외부 서비스·DB·네트워크에 닿지 않는다
출력 형식: JSON 객체만. {{"tests": ["<만든 테스트 파일 경로>"], "note": "한 줄"}}
"""

CHANGE_PROMPT = """목표: {summary} — 대상 {anchor}. 동작은 그대로 두고 정리한다.

참고: 고정 테스트 {tests}(고치지 않는다), 대상 코드.
제약:
- 동작을 바꾸지 않는다
- 구조를 바꾸지 않는다: 새 파일·파일 이동·모듈 간 이동·공개 인터페이스(이름·인자·반환) 변경 없음
- 테스트 파일은 건드리지 않는다
- 변경은 작게(diff {max_lines}줄 이하)
- 보호 영역: {protected}
출력 형식: JSON 객체만. {{"done": true|false, "note": "한 줄"}}
"""


def _link_node_modules(root, wt):
    src = Path(root) / "node_modules"
    dst = Path(wt) / "node_modules"
    if not src.is_dir() or dst.exists():
        return
    try:
        if sys.platform == "win32":
            from vmc_harness.core.proc import run
            run(["cmd", "/c", "mklink", "/J", str(dst), str(src)])
        else:
            os.symlink(str(src), str(dst))
    except OSError:
        pass
    # worktree 안에서 링크가 변경으로 잡히지 않게
    try:
        ex = Path(wt) / ".git"
        if ex.is_file():
            gitdir = ex.read_text(encoding="utf-8").split(":", 1)[1].strip()
            info = Path(gitdir) / "info"
            info.mkdir(parents=True, exist_ok=True)
            with (info / "exclude").open("a", encoding="utf-8") as f:
                f.write("\n/node_modules\n")
    except (OSError, IndexError):
        pass


def _anchor_str(a):
    return "%s%s" % (a.get("path"), ("::" + a["symbol"]) if a.get("symbol") else "")


def _propose(root, cand, reason):
    from vmc_harness.core import notices
    a = cand.get("anchor") or {}
    notices.add(root, "cleansing_proposal", "flow",
                "정리 제안: %s (%s)" % (cand.get("summary"), _anchor_str(a)),
                area=a.get("path"), refs={"candidate_id": cand["candidate_id"], "reason": reason})
    ledger.update_candidate(root, cand["candidate_id"], status="proposed", proposed_at=now_iso(), proposed_reason=reason)
    return {"outcome": "proposed", "reason": reason, "tokens": 0}


def process(root, cand, deadline=None):
    """후보 1건. {"outcome": prepared|proposed|resolved|expired|skipped|discarded|yielded|failed|archived, "reason", "tokens", "action_id"?}
    이미 준비한 조치가 머지를 기다리면(브랜치 있음) 다시 만들지 않는다. 같은 이유로 연속 실패하면 보관(archived)."""
    pending = prepared_pending(root, cand)
    if pending:
        return {"outcome": "skipped", "reason": "준비된 조치 머지 대기", "tokens": 0, "action_id": pending["action_id"]}
    res = _process(root, cand, deadline=deadline)
    if res.get("outcome") in ("failed", "discarded"):
        res = _record_failure(root, cand, res)
    return res


def prepared_pending(root, cand):
    """이 후보로 준비한 조치(정리·안전망)가 아직 prepared이고 브랜치가 살아 있으면 그 조치. 브랜치가 지워졌으면 None(다시 준비)."""
    for aid in (cand.get("action_id"), cand.get("safety_net_action")):
        a = ledger.get_action(root, aid) if aid else None
        if a and a.get("status") == "prepared" and a.get("branch") \
                and gitops.rev(root, "refs/heads/%s" % a["branch"]):
            return a
    return None


def _record_failure(root, cand, res):
    """실패·버림을 후보에 남기고, 끝에서부터 같은 이유가 loop.cleanse_same_reason_max번 이어지면 보관 + 알림.
    실행 실패(로그인·claude 없음·worktree)는 세지 않는다(티켓과 같다)."""
    reason = str(res.get("reason") or "")
    if candidates.not_counted(reason):
        return res
    cid = cand["candidate_id"]
    c = ledger.get_candidate(root, cid) or cand
    atts = list(c.get("attempts") or [])
    atts.append({"at": now_iso(), "outcome": res.get("outcome"), "reason": reason[:200],
                 "tokens": int(res.get("tokens") or 0)})
    streak = 0
    for x in reversed(atts):
        if x.get("reason") != atts[-1]["reason"]:
            break
        streak += 1
    fields = {"attempts": atts, "last_failure": reason[:200]}
    limit = config.get("loop.cleanse_same_reason_max", root)
    archived = streak >= limit
    if archived:
        fields.update(status="archived", archived_at=now_iso(), resolved_reason="same_failure")
    ledger.update_candidate(root, cid, **fields)
    if not archived:
        return res
    from vmc_harness.core import notices
    a = cand.get("anchor") or {}
    notices.add(root, "cleansing_archived", "flow",
                "정리 후보 '%s'이(가) 같은 이유로 %d번 연속 안 됨 — 보관, 다시 시도하지 않음(이유: %s)" % (
                    cand.get("summary"), streak, reason[:80]),
                area=a.get("path"), refs={"candidate_id": cid, "attempts": len(atts), "reason": reason[:200]})
    return dict(res, outcome="archived")


def _process(root, cand, deadline=None):
    cid = cand["candidate_id"]
    anchor = cand.get("anchor") or {}
    change = cand.get("change") or "internal_cleanup"
    main = gitops.main_branch(root)
    main_sha = gitops.rev(root, main)
    if candidates.expire_if_stale(root, cand, main):
        return {"outcome": "expired", "reason": "사람 커밋 누적", "tokens": 0}
    if anchor.get("path") in candidates.busy_files(root, main):
        return {"outcome": "skipped", "reason": "작업 중인 영역", "tokens": 0}
    if candidates.is_protected(root, anchor.get("path") or ""):
        ledger.update_candidate(root, cid, status="expired", resolved_reason="protected", resolved_at=now_iso())
        return {"outcome": "expired", "reason": "보호 영역", "tokens": 0}
    name = cid.lower()
    wt, branch = gitops.add_worktree(root, name, main_sha)
    if wt is None:
        return {"outcome": "failed", "reason": "worktree: %s" % branch, "tokens": 0}
    tokens = 0
    keep_branch = False
    try:
        # 낡음: 최신 main에서 재확인
        if candidates.refresh(root, cand, wt) == "resolved":
            return {"outcome": "resolved", "reason": "자연 해소", "tokens": 0}
        if area_busy(root, anchor.get("path")):
            return {"outcome": "skipped", "reason": "같은 영역 조치 시험 중", "tokens": 0}
        if change in candidates.PROPOSE or (change != "add_pinning_test"
                                             and candidates.is_write_path(wt, anchor.get("path"), anchor.get("symbol"))):
            return _propose(root, cand, "구조 변경·쓰기 경로")
        _link_node_modules(root, wt)
        protected = ", ".join(candidates.protected_globs(root)) or "없음"
        mode = change
        if change == "internal_cleanup" and not candidates.covered_by_tests(wt, anchor):
            mode = "add_pinning_test"  # 안전망 먼저
            ledger.update_candidate(root, cid, safety_net_first=True)
        pin_commit, pin_tests = None, []
        if mode in ("internal_cleanup", "add_pinning_test"):
            from vmc_harness.loop import fixation
            lang = "python" if (anchor.get("path") or "").endswith(".py") else "other"
            r = claude(root, PIN_PROMPT.format(anchor=_anchor_str(anchor), runner=fixation.runner_hint(root, wt, lang)),
                       wt, EDIT_TOOLS, deadline=deadline)
            tokens += r["tokens"]
            if r.get("interrupted"):
                return {"outcome": "yielded", "reason": r["interrupted"], "tokens": tokens}
            if not r["ok"]:
                return {"outcome": "failed", "reason": "고정 테스트 작성 실패", "tokens": tokens}
            pin_tests = [f for f in _status_files(wt)]
            if not pin_tests or not all(detect.is_test_path(f) for f in pin_tests):
                return {"outcome": "discarded", "reason": "고정 테스트 단계가 테스트 밖을 고침", "tokens": tokens}
            pin_commit = gitops.commit_all(wt, "test(vmc-harness): 현재 동작 고정 — %s" % _anchor_str(anchor))
            if not pin_commit:
                return {"outcome": "failed", "reason": "고정 테스트 없음", "tokens": tokens}
        if activity.stop_requested(root):
            return {"outcome": "yielded", "reason": "stop", "tokens": tokens}
        if mode != "add_pinning_test":
            r = claude(root, CHANGE_PROMPT.format(summary=cand.get("summary"), anchor=_anchor_str(anchor),
                                                  tests=", ".join(pin_tests) or "없음", protected=protected,
                                                  max_lines=config.get("loop.max_change_lines", root)),
                       wt, EDIT_TOOLS, deadline=deadline)
            tokens += r["tokens"]
            if r.get("interrupted"):
                return {"outcome": "yielded", "reason": r["interrupted"], "tokens": tokens}
            if not r["ok"]:
                return {"outcome": "failed", "reason": "정리 실패", "tokens": tokens}
            touched = _status_files(wt)
            if any(f in pin_tests or detect.is_test_path(f) for f in touched):
                return {"outcome": "discarded", "reason": "정리 단계가 테스트를 고침", "tokens": tokens}
            change_commit = gitops.commit_all(wt, "refactor(vmc-harness): %s" % cand.get("summary"))
            if not change_commit:
                return {"outcome": "resolved" if not candidates.run_recheck(wt, cand.get("recheck")) else "failed",
                        "reason": "바뀐 것 없음", "tokens": tokens}
        else:
            change_commit = pin_commit
        # main이 움직였으면 리베이스, 충돌이면 버리고 대기열
        cur_main = gitops.rev(root, main)
        if cur_main != main_sha:
            ok, _ = gitops.rebase_onto(wt, cur_main)
            if not ok:
                return {"outcome": "discarded", "reason": "main 충돌", "tokens": tokens}
            main_sha = cur_main
            commits = _commits(wt, main_sha)
            pin_commit = commits[0] if pin_commit and commits else pin_commit
            change_commit = commits[-1] if commits else change_commit
        from vmc_harness.loop import merge
        g = merge.grade(root, wt, main_sha, pin_commit if mode != "add_pinning_test" else None,
                        change_commit, cand, mode, deadline=deadline)
        if (g.get("reason") or "").startswith("interrupted:"):
            return {"outcome": "yielded", "reason": g["reason"], "tokens": tokens}
        if g["grade"] == "C":
            return _propose(root, cand, "쓰기 경로")
        if g["grade"] not in ("A", "B"):
            fx = (g.get("fixation") or {}).get("detail")
            if fx:
                ledger.update_candidate(root, cid, fixation_detail={k: str(v)[:200] for k, v in fx.items()})
            return dict(_propose(root, cand, "자동 머지 조건 미달: %s" % g["reason"]), tokens=tokens)
        # 준비 완료 → 장부(prepared) + 커밋 trailer
        try:
            action = _record_action(root, cand, mode, g, branch, main_sha, pin_tests, wt)
        except ledger.MetaGateError as e:
            return {"outcome": "skipped", "reason": str(e), "tokens": tokens}
        commits = _rewrite_trailers(wt, branch, main_sha, action["action_id"])
        ledger.update_action(root, action["action_id"], commits=commits)
        if mode == "add_pinning_test" and change != "add_pinning_test":
            ledger.update_candidate(root, cid, safety_net_action=action["action_id"])  # 정리는 그물 머지 뒤 다음 밤
        else:
            ledger.update_candidate(root, cid, status="prepared", action_id=action["action_id"])
        keep_branch = True
        return {"outcome": "prepared", "reason": None, "tokens": tokens, "action_id": action["action_id"],
                "grade": g["grade"]}
    finally:
        gitops.remove_worktree(root, name, delete_branch=not keep_branch)


def _status_files(wt):
    from vmc_harness.core.gitutil import git
    git(wt, "add", "-A", "-N")
    rc, out, _ = git(wt, "status", "--porcelain")
    files = []
    for line in out.splitlines() if rc == 0 else []:
        p = line[3:].strip().strip('"')
        if " -> " in p:
            p = p.split(" -> ", 1)[1]
        if p and p != "node_modules":
            files.append(p)
    return files


def _commits(wt, base):
    from vmc_harness.core.gitutil import git
    rc, out, _ = git(wt, "rev-list", "--reverse", "%s..HEAD" % base)
    return [l.strip() for l in out.splitlines() if l.strip()] if rc == 0 else []


def _rewrite_trailers(wt, branch, base, action_id):
    """루프 커밋 메시지에 `Harness-Action: <id>` trailer를 붙여 다시 만든다(내용·부모 관계 그대로)."""
    from vmc_harness.core.gitutil import git
    parent = base
    new = []
    for c in _commits(wt, base):
        rc, msg, _ = git(wt, "log", "-1", "--format=%B", c)
        rc2, tree, _ = git(wt, "rev-parse", "%s^{tree}" % c)
        body = msg.rstrip() + "\n\n%s: %s\n" % (gitops.TRAILER_ACTION, action_id)
        from vmc_harness.core.proc import run
        rc3, out, _ = run(["git", "commit-tree", tree.strip(), "-p", parent, "-F", "-"], cwd=str(wt), input_text=body)
        if rc3 != 0:
            return _commits(wt, base)
        parent = out.strip()
        new.append(parent)
    if new:
        git(wt, "update-ref", "refs/heads/%s" % branch, new[-1])
        git(wt, "reset", "-q", "--hard", new[-1])
    return new


def _record_action(root, cand, mode, g, branch, base, pin_tests, wt):
    """장부 조치(prepared). target은 ledger 노출 규약 {"area": 경로} — 고정 테스트만 추가한 조치는 그 테스트 파일이 영역."""
    a = cand.get("anchor") or {}
    area = pin_tests[0] if mode == "add_pinning_test" and pin_tests else a.get("path")
    hyp = cand.get("summary") if mode != "add_pinning_test" else "현재 동작 고정 테스트 추가 — %s" % _anchor_str(a)
    fx = g.get("fixation") or {}
    extra = {"grade": g["grade"], "branch": branch, "base": base, "candidate_id": cand["candidate_id"],
             "change": mode, "pin_tests": pin_tests, "area": area, "anchor": a,
             "module": detect.module_of(wt, a.get("path") or ""),
             "fixation": {k: fx.get(k) for k in ("1", "2", "3", "4")} if fx else None,
             "prepared_at": now_iso()}
    return ledger.create_action(root, {"type": "cleansing", "ref": cand["candidate_id"]}, "code", hyp,
                                target={"area": area}, status="prepared", extra=extra)


def area_busy(root, area):
    """같은 영역에 시험 중·머지 대기 조치가 있으면 True(같은 대상에는 시험 중인 조치 하나만 — 04)."""
    for x in ledger.list_actions(root):
        if x and x.get("status") in ("trial", "extended", "prepared") and (x.get("target") or {}).get("area") == area:
            return True
    return False
