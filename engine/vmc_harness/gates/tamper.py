"""검증물 변조 금지 (spec 02 — 안전, fail-closed).

기존 테스트·QA 시나리오·기대값 파일의 약화·삭제를 막는다. 새 테스트 파일 추가는 막지 않는다.
"기존" = 작업 착수 스냅샷 때 이미 있던 파일(task.scope.existed_at_base — 모든 게이트가 같은 기준). 이번 세션·작업이 새로 만든
파일은 커밋한 뒤에도 기존이 아니다(현장 결함 C2). 착수 선언이 없으면 세션 시작 HEAD 기준.
예외: 착수 선언 `test_change`({"files": [...], "reason": "..."})에 그 파일(또는 glob)과 이유가 미리 적힌 경우 — 완료 보고에 드러나도록 events에 남긴다.
기존 테스트 파일(test_globs) 수정은 약화 신호가 있을 때만 막는다(2026-11-17, 현장 결함 C12): 건너뛰기·한정 실행 표시 증가,
검증 호출 감소, 테스트 정의 감소(weakening). 신호 없는 수정(상한 낮추기·시그니처 따라가기·주석)은 통과시키고 이벤트 test_modified로
남겨 완료 보고·커밋(Harness-TestChange)에 드러낸다. 판정은 바뀌는 내용을 미리 알 수 있을 때만(Edit·MultiEdit·Write는 미리 적용,
Codex 패치는 지운 줄·더한 줄, 해석 못 한 셸 명령은 실행 뒤 대조) — 해석한 셸 쓰기(sed -i·리다이렉트 등)·삭제·이름 바꾸기와
기대값 파일(expected_globs — 스냅샷·골든·QA 시나리오)은 그대로 막는다.
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


def _count(text, patterns, word=False):
    """문자열 그대로 센다. word면 영문자·숫자로 시작하는 항목은 앞 글자가 식별자 글자·점이 아닐 때만(it( ≠ submit()."""
    n = 0
    for p in patterns or []:
        p = str(p)
        if not p:
            continue
        if word and (p[0].isalnum() or p[0] == "_"):
            n += len(re.findall(r"(?<![A-Za-z0-9_.])" + re.escape(p), text or ""))
        else:
            n += (text or "").count(p)
    return n


def _skip_count(text, root=None):
    return sum(len(rx.findall(text or "")) for rx in _skip_rx(root))


def weakening(before, after, root=None):
    """기존 테스트 파일 변경에서 약화 신호 — 오탐 없는 좁은 신호만(spec 02, 2026-11-17).
    - skip_added: 건너뛰기·한정 실행 표시(gates.tamper_skip_patterns)가 늘었다
    - assertions_reduced: 검증 호출(gates.tamper_assert_patterns)이 줄었다
    - tests_reduced: 테스트 정의(gates.tamper_test_def_patterns)가 줄었다
    판정할 수 없으면(before·after 없음) None. 기대값 리터럴만 바꾸는 약화는 잡지 않는다 — 막는 대신 완료 보고·커밋에
    "기존 테스트 수정"으로 드러낸다. 옛 규칙(더하기만 허용)은 상한 낮추기·시그니처 따라가기·주석 수정까지 막았다(현장 결함 C12)."""
    if before is None or after is None:
        return None
    out = []
    if _skip_count(after, root) > _skip_count(before, root):
        out.append("skip_added")
    ap = config.get("gates.tamper_assert_patterns", root)
    if _count(after, ap) < _count(before, ap):
        out.append("assertions_reduced")
    tp = config.get("gates.tamper_test_def_patterns", root)
    if _count(after, tp, word=True) < _count(before, tp, word=True):
        out.append("tests_reduced")
    return out


WEAK_LABEL = {"skip_added": "건너뛰기 표시 추가", "assertions_reduced": "검증 호출 감소", "tests_reduced": "테스트 정의 감소"}


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


def _lf(text):
    return text.replace("\r\n", "\n") if isinstance(text, str) else text


def _edits_apply(ctx, cur):
    """Edit·MultiEdit를 미리 적용한 내용. old_string이 차례로 다 찾아지지 않으면 None(판정 불가).
    줄끝은 LF로 맞춰 비교한다 — Windows 작업 트리(CRLF)에 에이전트가 LF로 old_string을 보내도 찾게(도구도 그렇게 맞춘다)."""
    ti = ctx.tool_input or {}
    if ctx.tool_name == "Write":
        return _lf(ti.get("content"))
    if cur is None:
        return None
    cur = _lf(cur)
    edits = [ti] if ctx.tool_name == "Edit" else list(ti.get("edits") or [])
    for e in edits:
        old = _lf(e.get("old_string") or "")
        if not old or old not in cur:
            return None
        new = _lf(e.get("new_string", ""))
        cur = cur.replace(old, new) if e.get("replace_all") else cur.replace(old, new, 1)
    return cur if edits else None


def _call_change(ctx, op):
    """이번 도구 호출이 그 기존 테스트 파일을 어떻게 바꾸는가(미리 적용·패치 판독) — (before, after) 또는 None(판정 불가)."""
    if op.get("op") not in ("edit", "write") or not is_test_file(ctx.root, op["rel"]):
        return None
    root, rel = ctx.root, op["rel"]
    if ctx.tool_name in ("Edit", "MultiEdit", "Write"):
        before = read_text_any(Path(root) / rel)
        after = _edits_apply(ctx, before)
        if before is None or after is None:
            return None
        return _lf(before), after
    if ctx.tool_name == "apply_patch":
        ti = ctx.tool_input or {}
        body = ti.get("command") or ti.get("patch") or ti.get("input") or ""
        if isinstance(body, list):
            body = "\n".join(str(x) for x in body)
        sec = _patch_section(body, op.get("raw"))
        if sec is None:
            return None
        # 패치 구역의 지운 줄·더한 줄만 비교한다(앞뒤 문맥 줄은 양쪽에 같아 개수 비교에 영향이 없다)
        return ("\n".join(ln[1:] for ln in sec if ln.startswith("-")),
                "\n".join(ln[1:] for ln in sec if ln.startswith("+")))
    return None


def _call_ok(ctx, op):
    """이번 호출이 그 기존 테스트 파일을 약화하지 않는가 — 판정할 수 없으면 False(막는다, 안전 게이트 fail-closed).
    더하기만이 아닌 통과 변경은 ctx.state["test_modified"]에 모아 완료 보고·커밋 표시로 넘긴다."""
    ch = _call_change(ctx, op)
    if ch is None:
        return False
    w = weakening(ch[0], ch[1], ctx.root)
    if w:
        ctx.state.setdefault("tamper_weak", {})[op["rel"]] = w
        return False
    if added_lines(*ch) is None:
        ctx.state.setdefault("test_modified", []).append(op["rel"])
    return True


def _tracked_under(root, rel):
    # 지울 대상에 와일드카드가 있으면(`rm -f *.png`) 그 폴더 안만 — git 기본 pathspec의 `*`는 `/`도 넘어 하위 폴더 전체를 잡는다
    spec = (":(glob)" + rel) if any(c in rel for c in "*?[") else rel
    rc, out, _ = git(root, "ls-files", "--", spec)
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
            if _in_head(f) and not (f == rel and _call_ok(ctx, o)):
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
    _note_modified(ctx, task)
    if not hits:
        return None
    blocked = [h for h in hits if not _declared(task, h)]
    ctx.state["block_paths"] = blocked   # "그냥 진행"이 이 파일들을 작업 끝까지 허용(gates.state.file_bypassed)
    if not blocked:
        try:
            log_event(ctx.root, "test_change_used", task_id=(task or {}).get("task_id"), files=hits)
        except Exception:
            pass
        return None
    weak = ctx.state.get("tamper_weak") or {}
    sig = sorted({WEAK_LABEL[k] for h in blocked for k in weak.get(h, [])})
    what = ("기존 테스트 약화(%s)" % "·".join(sig)) if sig else "기존 테스트·QA 시나리오·기대값 수정·삭제"
    return block("검증물 변조 차단: %s (%s)" % (what, short_list(blocked)), "검증물 변조 차단: %s" % what)


def _note_modified(ctx, task):
    """약화 신호 없이 통과한 기존 테스트 수정 — 이벤트로 남겨 완료 보고·커밋 표시(Harness-TestChange)가 읽는다."""
    mod = ctx.state.get("test_modified")
    if mod:
        log_event(ctx.root, "test_modified", task_id=(task or {}).get("task_id"), session_id=ctx.session_id,
                  files=sorted(set(mod)))
