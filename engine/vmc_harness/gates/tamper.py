"""검증물 변조 금지 (spec 02 — 안전, fail-closed).

기존 테스트·QA 시나리오·기대값 파일의 수정·삭제를 막는다. 새 테스트 파일 추가는 막지 않는다.
"기존" = 작업 착수 스냅샷 때 이미 있던 파일(task.scope.existed_at_base — 모든 게이트가 같은 기준). 이번 세션·작업이 새로 만든
파일은 커밋한 뒤에도 기존이 아니다(현장 결함 C2). 착수 선언이 없으면 세션 시작 HEAD 기준.
예외: 착수 선언 `test_change`({"files": [...], "reason": "..."})에 그 파일(또는 glob)과 이유가 미리 적힌 경우 — 완료 보고에 드러나도록 events에 남긴다.
"""

import re

from vmc_harness.core.gitutil import git
from vmc_harness.gates import globs
from vmc_harness.gates.common import block, is_verification, log_event, short_list

NAME = "tamper"
_PATHLIKE = re.compile(r"[\w./\\-]+\.[A-Za-z0-9]+")


def _existing(root, ctx, task):
    """rel → 착수(없으면 세션 시작) 때 이미 있던 파일인가. 같은 호출 안에서 캐시."""
    from vmc_harness.task import scope
    if task and isinstance(task.get("base"), dict):
        snap, created = task["base"], task.get("created") or []
    else:
        from vmc_harness.gates import state
        snap, created = {"head": state.load(root, ctx.session_id).get("base_head") or "HEAD"}, []
    cache = {}

    def f(rel):
        if rel not in cache:
            cache[rel] = scope.existed_at_base(root, snap, rel, created)
        return cache[rel]
    return f


def _tracked_under(root, rel):
    rc, out, _ = git(root, "ls-files", "--", rel)
    return [l.strip() for l in out.splitlines() if l.strip()] if rc == 0 else []


def _declared(task, rel):
    tc = ((task or {}).get("declare") or {}).get("test_change")
    if not isinstance(tc, dict) or not (tc.get("reason") or "").strip():
        return False
    for f in tc.get("files") or []:
        if globs.match(rel, f) or globs.norm(f) == rel:
            return True
    return False


def targets(ctx, task=None):
    """이번 호출이 고치거나 지우는 기존 검증물 목록."""
    root = ctx.root
    _in_head = _existing(root, ctx, task)
    hits = []
    for o in ctx.state.get("file_ops") or []:
        rel = o.get("rel")
        if not rel:
            continue
        files = [rel]
        if o["op"] == "delete":
            files = [f for f in _tracked_under(root, rel) if f != rel] or [rel]
        for f in files:
            if not is_verification(root, f):
                continue
            if o["op"] == "write" and not _in_head(f):
                continue   # 새 파일 추가
            if _in_head(f):
                hits.append(f)
    if ctx.command and ctx.state.get("shell_opaque") and ctx.state.get("opaque_watch") is not True:
        # 실행 전 기록을 했으면 실행 뒤 대조(opaque_watch)가 판정한다 — 못 했을 때만 옛 문자열 짐작
        for tok in _PATHLIKE.findall(ctx.command):
            rel = globs.rel_to(root, tok, ctx.cwd)
            if rel and is_verification(root, rel) and _in_head(rel):
                hits.append(rel)
    out = []
    for h in hits:
        if h not in out:
            out.append(h)
    return out


def check(ctx, task):
    hits = targets(ctx, task)
    if not hits:
        return None
    blocked = [h for h in hits if not _declared(task, h)]
    if not blocked:
        try:
            log_event(ctx.root, "test_change_used", task_id=(task or {}).get("task_id"), files=hits)
        except Exception:
            pass
        return None
    return block("검증물 변조 차단: 기존 테스트·QA 시나리오·기대값 수정·삭제 (%s)" % short_list(blocked),
                 "검증물 변조 차단: 기존 테스트·QA 시나리오·기대값 수정·삭제")
