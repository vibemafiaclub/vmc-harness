"""검증물 변조 금지 (spec 02 — 안전, fail-closed).

기존 테스트·QA 시나리오·기대값 파일의 수정·삭제를 막는다. 새 테스트 파일 추가는 막지 않는다.
"기존" = 작업 착수 스냅샷 때 이미 있던 파일(task.scope.existed_at_base — 모든 게이트가 같은 기준). 이번 세션·작업이 새로 만든
파일은 커밋한 뒤에도 기존이 아니다(현장 결함 C2). 착수 선언이 없으면 세션 시작 HEAD 기준.
예외: 착수 선언 `test_change`({"files": [...], "reason": "..."})에 그 파일(또는 glob)과 이유가 미리 적힌 경우 — 완료 보고에 드러나도록 events에 남긴다.
더하기만 하는 변경은 막지 않는다: 기존 줄을 순서대로 모두 남기고 줄만 더했으며(Edit·MultiEdit·Write는 미리 적용해서, Codex 패치는
`-` 줄 없음으로, 해석 못 한 셸 명령은 실행 뒤 대조로), 더한 줄에 건너뛰기·한정 실행 표시(gates.tamper_skip_patterns)가 없을 때.
에이전트가 기존 테스트 파일에 테스트를 더하려다 막혀 새 파일로 쪼개던 현장 결함(6건 중 5건). 해석한 셸 쓰기(sed -i·리다이렉트 등)는 그대로 막는다.
"""

import re
from pathlib import Path

from vmc_harness.core import config
from vmc_harness.core.gitutil import git
from vmc_harness.core.jsonio import read_text_any
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


def _skip_rx(root=None):
    out = []
    for p in config.get("gates.tamper_skip_patterns", root) or []:
        p = str(p)
        if p:
            out.append(re.compile((r"(?<![A-Za-z0-9_.])" if (p[0].isalnum() or p[0] == "_") else "") + re.escape(p)))
    return out


def added_lines(before, after):
    """기존 줄을 순서대로 모두 남긴 변경이면 더한 줄 목록, 아니면 None(고치거나 지운 줄이 있음)."""
    if before is None or after is None:
        return None
    old, new = before.splitlines(), after.splitlines()
    i, added = 0, []
    for ln in new:
        if i < len(old) and ln == old[i]:
            i += 1
        else:
            added.append(ln)
    return added if i == len(old) else None


def skip_marks(lines, root=None):
    """더한 줄에 있는 건너뛰기·한정 실행 표시."""
    hits = []
    for rx in _skip_rx(root):
        if any(rx.search(ln) for ln in lines) and rx.pattern not in hits:
            hits.append(rx.pattern)
    return hits


def additive_ok(before, after, root=None):
    """더하기만 하고 건너뛰기 표시를 더하지 않은 변경인가."""
    added = added_lines(before, after)
    return added is not None and not skip_marks(added, root)


def _patch_section(body, raw):
    """Codex apply_patch 본문에서 그 파일의 Update 구역 줄들. 없으면 None."""
    lines = (body or "").replace("\r\n", "\n").split("\n")
    out, inside = None, False
    for ln in lines:
        if ln.startswith("*** "):
            if inside and not ln.startswith("*** End of File"):
                if ln.startswith("*** Move to:"):
                    return None       # 이름 바꾸기 — 더하기만이 아니다
                inside = False
            if ln.strip() == "*** Update File: %s" % raw:
                inside, out = True, []
            continue
        if inside:
            out.append(ln)
    return out


def is_test_file(root, rel):
    """더하기 예외는 테스트 파일(test_globs)만 — 기대값·스냅샷·QA 시나리오에 줄을 더하면 기대가 바뀐다."""
    from vmc_harness.gates.common import verification_globs
    tg, eg = verification_globs(root)
    return bool(globs.match_any(rel, tg)) and not globs.match_any(rel, eg)


def _call_additive(ctx, op):
    """이번 도구 호출이 그 기존 테스트 파일에 더하기만 하는가(미리 적용·패치 판독). 판정할 수 없으면 False."""
    if op.get("op") not in ("edit", "write") or not is_test_file(ctx.root, op["rel"]):
        return False
    root, rel = ctx.root, op["rel"]
    if ctx.tool_name in ("Edit", "MultiEdit", "Write"):
        from vmc_harness.gates import meta
        return additive_ok(read_text_any(Path(root) / rel), meta.simulate(ctx, rel), root)
    if ctx.tool_name == "apply_patch":
        ti = ctx.tool_input or {}
        body = ti.get("command") or ti.get("patch") or ti.get("input") or ""
        if isinstance(body, list):
            body = "\n".join(str(x) for x in body)
        sec = _patch_section(body, op.get("raw"))
        if sec is None or any(ln.startswith("-") for ln in sec):
            return False
        return not skip_marks([ln[1:] for ln in sec if ln.startswith("+")], root)
    return False


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
            if _in_head(f) and not (f == rel and _call_additive(ctx, o)):
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
