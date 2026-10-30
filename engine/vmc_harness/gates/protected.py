"""보호 영역 (spec 02 — 안전, fail-closed).

`.harness/protected.json` 형식(부트스트랩이 제안, 사람이 [추가]로 확정):
    {"paths": ["auth/**", {"glob": "be/**/work_requests/**", "kind": "approval_flow", "basis": {...}}, ...]}
    (문자열 목록만 있는 옛 형식도 읽는다)
kind: audit | approval_flow | auth | clock | generated | db_write | prod_config — approval_flow(결재 흐름)는 완료 게이트가 QA 시나리오 증거를 요구한다.
prod_config(운영 접속 설정)는 쓰기 승인에 더해 읽기·그 설정으로 앱 실행도 막힌다(gates.prod_config).

- 보호 경로 수정은 그 작업의 승인 카드(core.ledger.create_approval, 사람 개입 기준 2)가 승인돼야 통과
- 착수 선언에 protected_touch가 있으면 선언 직후 카드 한 번(작업 시작에 묻는다), 작업 중엔 새 카드를 만들지 않는다
- 선언 없이 닿으면 막고 그때 카드 한 번. 승인 대기 중이면 막기만(다시 묻지 않음)
"""

from vmc_harness.core import ledger
from vmc_harness.core.jsonio import read_json
from vmc_harness.core import paths
from vmc_harness.gates import globs
from vmc_harness.gates.common import block, short_list

NAME = "protected"
DETAIL_KIND = "protected"


def entries(root):
    data = read_json(paths.harness_dir(root) / "protected.json", None)
    if data is None:
        return []
    items = data.get("paths", []) if isinstance(data, dict) else data
    out = []
    for it in items or []:
        if isinstance(it, str):
            out.append({"glob": it, "kind": None})
        elif isinstance(it, dict) and it.get("glob"):
            e = dict(it)
            if e.get("approval_flow"):
                e["kind"] = "approval_flow"
            out.append(e)
    return out


def matched(root, rels, ents=None):
    """{glob: [rel...]} — 보호 경로에 걸린 파일."""
    ents = entries(root) if ents is None else ents
    out = {}
    for rel in rels or []:
        for e in ents:
            if globs.match(rel, e["glob"]):
                out.setdefault(e["glob"], []).append(rel)
    return out


def is_protected(root, rel):
    return bool(matched(root, [globs.norm(rel)]))


def approval_flow_globs(root):
    return [e["glob"] for e in entries(root) if e.get("kind") == "approval_flow"]


def _task_approvals(root, task_id):
    return [a for a in ledger.list_approvals(root)
            if a and a.get("task_id") == task_id and (a.get("detail") or {}).get("kind") == DETAIL_KIND]


def _create(root, task, globs_needed, paths_hit, declared):
    scope = ", ".join((task.get("declare") or {}).get("scope") or []) or "-"
    return ledger.create_approval(
        root, task["task_id"], 2,
        decision_text="보호 영역(%s) 코드를 이번 작업에서 고칠지" % short_list(globs_needed),
        recommend="approve" if declared else "check",
        reversible=True,
        revert_how="이 작업 커밋을 git revert",
        verification=["착수 선언 범위: %s" % scope] + (["보호 경로: %s" % short_list(paths_hit)] if paths_hit else []),
        diff_lines=0,
        risk="감사·결재·인증 등 보호 영역 동작이 바뀐다",
        detail={"kind": DETAIL_KIND, "globs": sorted(globs_needed), "paths": sorted(paths_hit or []), "declared": bool(declared)})


def ensure_declared_approval(root, task):
    """착수 선언이 protected_touch면 작업 시작에 승인 카드 한 번. 이미 있으면 아무것도 안 한다."""
    if not task or not task.get("task_id"):
        return None
    decl = task.get("declare") or {}
    if not decl.get("protected_touch"):
        return None
    if _task_approvals(root, task["task_id"]):
        return None
    ents = entries(root)
    scope = decl.get("scope") or []
    hit = set()
    for e in ents:
        prefix = e["glob"].split("*")[0].rstrip("/")
        for s in scope:
            s2 = globs.norm(s).rstrip("/")
            if globs.match(s2, e["glob"]) or (prefix and (prefix.startswith(s2) or s2.startswith(prefix))):
                hit.add(e["glob"])
    if not hit:
        hit = {e["glob"] for e in ents}
    if not hit:
        return None
    return _create(root, task, hit, [], True)


def check(ctx, task):
    rels = list(ctx.write_paths or [])
    if not rels:
        return None
    m = matched(ctx.root, rels)
    if not m:
        return None
    need = set(m)
    hit_paths = sorted({p for v in m.values() for p in v})
    if not task or not task.get("task_id"):
        return block("보호 영역 수정 차단: %s" % short_list(hit_paths), "보호 영역 수정 차단")
    aps = _task_approvals(ctx.root, task["task_id"])

    def covers(a):
        return need <= set((a.get("detail") or {}).get("globs") or [])

    approved = [a for a in aps if a.get("answer") == "approve"]
    if approved:
        ok_globs = set()
        for a in approved:
            ok_globs |= set((a.get("detail") or {}).get("globs") or [])
        if need <= ok_globs:
            return None
    rejected = [a for a in aps if a.get("answer") == "reject" and covers(a)]
    if rejected:
        return block("보호 영역 수정 거절됨(%s): %s" % (rejected[-1]["approval_id"], short_list(hit_paths)),
                     "보호 영역 수정 거절됨(%s)" % rejected[-1]["approval_id"])
    pending = [a for a in aps if not a.get("answer")]
    if pending:
        # 작업 중 재질문 없음 — 기존 카드에 이번 경로가 없으면 막기만 하고 새 카드는 만들지 않는다(선언 때 물었다)
        return block("보호 영역 수정 승인 대기(%s): %s" % (pending[-1]["approval_id"], short_list(hit_paths)),
                     "보호 영역 수정 승인 대기(%s)" % pending[-1]["approval_id"])
    declared = bool((task.get("declare") or {}).get("protected_touch"))
    try:
        a = _create(ctx.root, task, need, hit_paths, declared)
        ref = a["approval_id"]
    except ledger.MetaGateError:
        ref = "-"
    ctx.state["approval_created"] = ref
    return block("보호 영역 수정 승인 필요(%s): %s" % (ref, short_list(hit_paths)), "보호 영역 수정 승인 필요(%s)" % ref)
