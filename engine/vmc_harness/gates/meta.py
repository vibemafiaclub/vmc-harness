"""메타 게이트 — L1 diff 조이기/풀기 판정 (spec 01 P3·00 사람 개입 기준 2).

L1 파일(`.harness/gates/*.json`, `.harness/protected.json`, `.harness/config.json`)의 바뀌기 전/후를 비교해
- 조이기(게이트 추가·발동 조건 넓히기·보호 경로 늘리기·엄격한 프로파일) → 자동 허용
- 풀기·지우기(게이트 삭제·끄기·발동 조건 좁히기) → 그 게이트에 헛막기 등 명시적 finding이 있거나, 대상 파일이 사라져 하네스가 끈 경우만
- 보호 영역 목록 줄이기·느슨한 프로파일·운영 판정 허용 칸(config.json prod_allow) 늘리기 → 사람 승인
  (approval detail.kind = protected_shrink | profile_loosen | prod_allow, answer=approve)
판정은 PreToolUse(에이전트의 Write·Edit 미리 적용)와 Stop(작업 트리 전체 L1 diff)에서 한다.
"""

import json
from pathlib import Path

from vmc_harness.core import config, ledger
from vmc_harness.core.gitutil import git
from vmc_harness.core.jsonio import read_text_any
from vmc_harness.gates import globs

NAME = "meta"
PROFILE_STRICTNESS = {"prototype": 0, "solo": 0, "standard": 1, "gxp": 2}
LOOSEN_FINDINGS = ("false_block", "revert", "reject", "button")
LOOSEN_KINDS = ("protected_shrink", "profile_loosen", "prod_allow")


def _prod_allow_items(cfg):
    """config.json prod_allow의 칸 — 'names:<이름>'·'files:<glob>'."""
    v = (cfg or {}).get("prod_allow")
    if not isinstance(v, dict):
        return set()
    out = set()
    for k in ("names", "files"):
        x = v.get(k) or []
        for i in (x if isinstance(x, list) else [x]):
            out.add("%s:%s" % (k, i))
    return out


def is_l1(rel):
    rel = globs.norm(rel or "")
    return (rel.startswith(".harness/gates/") and rel.endswith(".json")) or rel in (".harness/protected.json", ".harness/config.json")


def head_text(root, rel):
    rc, out, _ = git(root, "show", "HEAD:%s" % rel)
    return out if rc == 0 else None


def _load(text):
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return {"__invalid__": True}


def _gate_active(meta):
    return bool(meta) and meta.get("enabled", True) and meta.get("status") not in ("reverted", "disabled")


def _trigger_set(meta, key):
    return set((meta.get("trigger") or {}).get(key) or [])


def diff_gate(gate_id, before, after):
    """(kind, detail) kind: tighten|loosen|same"""
    if before is None and after is not None:
        return "tighten", "게이트 추가"
    if before is not None and after is None:
        return ("loosen", "게이트 삭제") if _gate_active(before) else ("same", "")
    if after.get("__invalid__"):
        return "loosen", "게이트 메타 형식 오류"
    if _gate_active(before) and not _gate_active(after):
        return "loosen", "게이트 끄기"
    if not _gate_active(before) and _gate_active(after):
        return "tighten", "게이트 켜기"
    loosen = []
    for key in ("paths", "events", "sizes", "change_kinds"):
        b, a = _trigger_set(before, key), _trigger_set(after, key)
        if b and a and not (b <= a):
            loosen.append("발동 조건 %s 좁힘" % key)
        if b and not a and key in ("events",):
            loosen.append("발동 조건 %s 비움" % key)
    if (before.get("basis") or {}) != (after.get("basis") or {}) and before.get("basis"):
        loosen.append("근거 변경")
    if loosen:
        return "loosen", ", ".join(loosen)
    return "same", ""


def _protected_globs(data):
    if data is None:
        return set()
    items = data.get("paths", []) if isinstance(data, dict) else data
    out = set()
    for it in items or []:
        if isinstance(it, str):
            out.add(it)
        elif isinstance(it, dict) and it.get("glob"):
            out.add(it["glob"])
    return out


def _has_loosen_evidence(root, gate_id, after):
    if after and after.get("disabled_reason") == "target_missing":
        return True
    meta_action = (after or {}).get("action_id")
    for f in ledger.list_findings(root):
        if not f or f.get("kind") not in LOOSEN_FINDINGS:
            continue
        t = f.get("target") or {}
        if t.get("gate_id") == gate_id or (meta_action and t.get("action_id") == meta_action):
            return True
    return False


def _human_approved(root, kind, items=None):
    for a in ledger.list_approvals(root):
        d = (a or {}).get("detail") or {}
        if d.get("kind") == kind and a.get("answer") == "approve":
            if items is None or set(items) <= set(d.get("items") or []):
                return True
    return False


def request_loosen(root, task_id, kind, items, reason):
    """안전 경계를 넓히는 L1 변경(보호 영역 목록 줄이기·느슨한 프로파일)의 승인 카드 — 사람 개입 기준 2.
    사람이 시킨 작업 안에서만(task_id 필수, P4). 승인되면 judge_file이 그 변경을 허용한다."""
    if kind not in LOOSEN_KINDS:
        raise ledger.MetaGateError("승인 요청 종류 오류: %s" % kind)
    if kind == "prod_allow" and not all(str(i).startswith(("names:", "files:")) and len(str(i).split(":", 1)[1]) for i in items):
        raise ledger.MetaGateError("prod_allow 항목은 names:<이름> 또는 files:<glob>")
    label = {"protected_shrink": "보호 영역 목록에서 %s 빼기", "profile_loosen": "프로파일을 %s(으)로 느슨하게",
             "prod_allow": "운영 판정에서 %s 빼기(운영 아님으로 허용)"}[kind]
    return ledger.create_approval(
        root, task_id, 2, decision_text=label % ", ".join(items), recommend="check", reversible=True,
        revert_how="L1 파일 변경 커밋을 git revert", verification=["요청 이유: %s" % (reason or "-")],
        diff_lines=len(items), risk="빠진 경로·완화된 설정은 안전 게이트가 더 이상 막지 않는다",
        detail={"kind": kind, "items": sorted(items)})


def _v(text, kind=None, items=None):
    return {"text": text, "kind": kind, "items": sorted(items or [])}


def judge_file_items(root, rel, before_text, after_text):
    """한 L1 파일 변경 판정 → 위반 목록 [{'text', 'kind', 'items'}]. kind가 있으면 사람 승인(request_loosen 종류)으로 풀린다."""
    rel = globs.norm(rel)
    before, after = _load(before_text), _load(after_text)
    out = []
    if rel.startswith(".harness/gates/"):
        gid = Path(rel).stem
        kind, detail = diff_gate(gid, before, after)
        if kind == "loosen" and not _has_loosen_evidence(root, gid, after or before):
            out.append(_v("게이트 '%s' 풀기(%s) — 헛막기 등 명시적 증거 없음" % (gid, detail)))
    elif rel == ".harness/protected.json":
        removed = sorted(_protected_globs(before) - _protected_globs(after))
        if removed and not _human_approved(root, "protected_shrink", removed):
            out.append(_v("보호 영역 목록 줄이기(%s) — 사람 승인 없음" % ", ".join(removed), "protected_shrink", removed))
    elif rel == ".harness/config.json":
        # 비교 기준이 없으면(HEAD에 아직 커밋되지 않은 설치 직후 설정) 지금 프로파일이 init에서 사람이 고른 기준이다 —
        # 없는 기준을 standard로 채워 solo·prototype 설치를 '느슨하게 바꿈'으로 막던 것(현장 결함: Stop 반복 차단)
        if before is not None:
            bp = before.get("profile", "standard")
            ap = (after or {}).get("profile", "standard")
            if PROFILE_STRICTNESS.get(ap, 1) < PROFILE_STRICTNESS.get(bp, 1) and not _human_approved(root, "profile_loosen", [ap]):
                out.append(_v("느슨한 프로파일로 변경(%s→%s) — 사람 승인 없음" % (bp, ap), "profile_loosen", [ap]))
        added = sorted(_prod_allow_items(after) - _prod_allow_items(before))
        if added and not _human_approved(root, "prod_allow", added):
            out.append(_v("운영 판정 허용 칸 늘리기(%s) — 사람 승인 없음" % ", ".join(added), "prod_allow", added))
        if before and after and before.get("project_id") != after.get("project_id"):
            out.append(_v("project_id 변경"))
    return out


def judge_file(root, rel, before_text, after_text):
    """한 L1 파일 변경 판정 → 위반 문장 목록(빈 목록 = 허용)."""
    return [v["text"] for v in judge_file_items(root, rel, before_text, after_text)]


def judge_worktree_items(root):
    """작업 트리의 L1 변경 전체(HEAD 대비) 판정 → 위반 목록(judge_file_items 형식)."""
    rc, out, _ = git(root, "status", "--porcelain", "--untracked-files=all", "--", ".harness")
    if rc != 0:
        return []
    viol = []
    for line in out.splitlines():
        path = line[3:].strip().strip('"')
        if " -> " in path:
            path = path.split(" -> ")[-1]
        if not is_l1(path):
            continue
        p = Path(root) / path
        after = p.read_text(encoding="utf-8") if p.is_file() else None
        viol.extend(judge_file_items(root, path, head_text(root, path), after))
    # 지워진 게이트 파일(.json)
    return viol


def judge_worktree(root):
    """작업 트리의 L1 변경 전체(HEAD 대비) 판정."""
    return [v["text"] for v in judge_worktree_items(root)]


# ---- 사람 승인(대화) — 메타 게이트가 막은 '사람 승인 없음' 위반을 승인 카드로 ----

def _open_card(root, kind, items):
    for a in ledger.list_approvals(root):
        d = (a or {}).get("detail") or {}
        if d.get("kind") == kind and not a.get("answer") and set(items) <= set(d.get("items") or []):
            return a
    return None


def pending_cards(root, task_key, viols):
    """승인으로 풀 수 있는 위반마다 승인 카드 id(같은 종류·항목의 답 없는 카드가 있으면 그것). 사람이 시킨 작업 키 필수(P4)."""
    ids = []
    for v in viols:
        if not v.get("kind") or not v.get("items"):
            continue
        a = _open_card(root, v["kind"], v["items"]) or request_loosen(root, task_key, v["kind"], v["items"], "메타 게이트 차단")
        if a["approval_id"] not in ids:
            ids.append(a["approval_id"])
    return ids


def _norm_phrase(s):
    return "".join(ch for ch in (s or "").lower() if not ch.isspace() and ch not in ".!~,。")


def is_approve_phrase(text, root=None):
    """사람 입력 전체가 승인 문구인가(공백·끝 문장부호 무시). '승인하지 마'처럼 문구를 품은 다른 말은 아니다."""
    t = _norm_phrase(text)
    return bool(t) and t in {_norm_phrase(p) for p in config.get("gates.approve_phrases", root)}


def approve_from_prompt(ctx):
    """UserPromptSubmit: 메타 게이트 차단 직후(gates.proceed_window_min 안) 사람이 '승인'이라고만 답하면 그 차단의 승인 카드를 승인.
    사람 입력 훅에서만 부른다 — 에이전트 셸에서 승인하는 길(vmc-harness approval)은 이 함수와 무관. 승인한 카드 id 목록."""
    from vmc_harness.gates import state
    if not is_approve_phrase(ctx.prompt, ctx.root):
        return []
    st = state.load(ctx.root, ctx.session_id)
    pend = st.get("meta_pending") or {}
    b = state.last_block(ctx.root, ctx.session_id)
    if not pend.get("approvals") or not b or b.get("gate") != NAME:
        return []
    mins = state.minutes_since(pend.get("at"))
    if mins is None or mins > float(config.get("gates.proceed_window_min", ctx.root)):
        return []
    done = []
    for aid in pend["approvals"]:
        try:
            ledger.mark_approval_shown(ctx.root, aid)
            rec = ledger.answer_approval(ctx.root, aid, "approve")
        except (KeyError, ledger.MetaGateError):
            continue
        if rec.get("answer") == "approve":
            done.append(aid)
    st["meta_pending"] = None
    state.save(ctx.root, ctx.session_id, st)
    return done


def simulate(ctx, rel):
    """에이전트 도구 호출이 끝난 뒤 내용(Write·Edit·MultiEdit). 판정할 수 없으면 None."""
    ti = ctx.tool_input or {}
    if ctx.tool_name == "Write":
        return ti.get("content")
    # UTF-16(BOM)도 읽고, 바이너리·다른 인코딩이면 판정할 수 없음(None) — 예외로 게이트를 깨지 않는다(현장 결함 F2)
    cur = read_text_any(Path(ctx.root) / rel)
    if ctx.tool_name == "Edit" and cur is not None:
        old, new = ti.get("old_string", ""), ti.get("new_string", "")
        return cur.replace(old, new) if ti.get("replace_all") else cur.replace(old, new, 1)
    if ctx.tool_name == "MultiEdit" and cur is not None:
        for e in ti.get("edits") or []:
            old, new = e.get("old_string", ""), e.get("new_string", "")
            cur = cur.replace(old, new) if e.get("replace_all") else cur.replace(old, new, 1)
        return cur
    return None


def check_pre(ctx):
    """PreToolUse 위반 문장 목록(check_pre_items의 text)."""
    return [v["text"] for v in check_pre_items(ctx)]


def check_pre_items(ctx):
    """PreToolUse: L1 파일을 고치는 호출이면 미리 적용해 판정. 셸·패치로 L1을 고치면 판정할 수 없어 막는다(안전)."""
    l1 = [o for o in (ctx.state.get("file_ops") or []) if o.get("rel") and is_l1(o["rel"])]
    if not l1:
        return []
    viol = []
    for o in l1:
        rel = o["rel"]
        p = Path(ctx.root) / rel
        before = p.read_text(encoding="utf-8") if p.is_file() else None
        if o["op"] == "delete":
            after = None
        else:
            after = simulate(ctx, rel)
            if after is None:
                if o["op"] in ("write",) and ctx.tool_name == "apply_patch":
                    continue   # 새 파일 추가(패치) — 조이기
                viol.append(_v("L1 파일(%s)을 판정할 수 없는 방식으로 수정" % rel))
                continue
        viol.extend(judge_file_items(ctx.root, rel, before, after))
    return viol
