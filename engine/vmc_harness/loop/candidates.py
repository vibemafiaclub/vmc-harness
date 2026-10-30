"""클렌징 후보 (spec 06·09) — 모델이 자유롭게 찾고, 받는 것은 기계 재확인 조건이 지금 참인 것만.

후보 제안 형식(모델 출력):
{"summary": "한 줄", "change": "<아래 종류>",
 "recheck": {"type": "grep|ast", "query": "<한 줄>", "path": "<grep 대상 glob, 선택>", "expect": "match|none"},
 "anchor": {"path": "<경로>", "symbol": "<심볼 이름 | null>"}}

recheck.ast 질의(한 줄):
  unreferenced <path>::<symbol>        심볼이 정의돼 있고 레포 어디서도 참조되지 않는다
  unreferenced_file <path>             파일이 있고 아무도 import·언급하지 않는다
  dup <path>::<symbol> <path>::<symbol>  두 함수의 정규화 AST 해시가 같다
  long_function <path>::<symbol> > <n> 함수 길이가 n줄 초과
  long_file <path> > <n>               파일 길이가 n줄 초과
  defined <path>::<symbol>             심볼이 정의돼 있다
recheck.grep: query = 정규식, expect = match(기본, 하나 이상 있음) | none(하나도 없음)

루프 권한(구조 지도를 바꾸는가):
  직접 — internal_cleanup(함수 안·같은 모듈 안 정리), remove_unreferenced(참조 0 제거), add_pinning_test(현재 동작 고정 테스트), comment_format
  제안만 — add_module, move, split, interface_change, cross_module_dedupe, write_path
"""

import fnmatch
import re
from pathlib import Path

from vmc_harness.core import config, ledger, paths
from vmc_harness.core.clock import now_iso
from vmc_harness.core.jsonio import read_json
from vmc_harness.loop import detect, gitops

DIRECT = ("internal_cleanup", "remove_unreferenced", "add_pinning_test", "comment_format")
PROPOSE = ("add_module", "move", "split", "interface_change", "cross_module_dedupe", "write_path")
CHANGES = DIRECT + PROPOSE

# 쓰기 경로(DB 쓰기·결재 상태 전이·SP·감사) — 넓게 잡는다(넓으면 제안으로 갈 뿐, 좁으면 위험)
WRITE_PATH_RE = re.compile(
    r"(?i)\b(insert\s+into|update\s+\S+\s+set|delete\s+from|merge\s+into|truncate\s+table|exec(ute)?\s+\w|"
    r"\.execute(many)?\s*\(|\.commit\s*\(|\.save\s*\(|\.bulk_create|session\.add|\.delete\s*\(|\.create\s*\(|"
    r"cursor\s*\(|sp_\w+|usp_\w+|audit|approve|finaliz|submit|transition|status\s*=)")


# ---- 보호 영역 ----

def protected_globs(root):
    data = read_json(paths.harness_dir(root) / "protected.json", None)
    if data is None:
        return []
    items = data.get("paths") if isinstance(data, dict) else data
    out = []
    for it in items or []:
        if isinstance(it, str):
            out.append(it)
        elif isinstance(it, dict) and (it.get("glob") or it.get("path")):
            out.append(it.get("glob") or it.get("path"))
    return out


def is_protected(root, rel):
    rel = str(rel).replace("\\", "/")
    try:
        from vmc_harness.gates.protected import is_protected as f
        if f(root, rel):
            return True
    except (ImportError, AttributeError):
        pass
    except Exception:
        return True  # 보호 판정 오류 = 보호로 본다(안전 쪽)
    for g in protected_globs(root):
        g = g.replace("\\", "/")
        if fnmatch.fnmatch(rel, g) or rel.startswith(g.rstrip("*").rstrip("/") + "/"):
            return True
    return False


def is_write_path(root, rel, symbol=None):
    """쓰기 경로 판정(기계·보수적). 패치 SQL 폴더·SQL 파일·쓰기 패턴이 보이는 코드."""
    rel = str(rel).replace("\\", "/")
    patch_dir = str(config.get("db.patch_dir", root)).strip("/")
    if rel.lower().endswith(".sql") or (patch_dir and (rel == patch_dir or rel.startswith(patch_dir + "/"))):
        return True
    src = detect.symbol_source(root, rel, symbol) if symbol else detect._read(root, rel)
    return bool(src and WRITE_PATH_RE.search(src))


# ---- 재확인 조건 ----

def _split_anchor(s):
    p, _, sym = s.partition("::")
    return p.strip(), (sym.strip() or None)


def run_recheck(root, recheck):
    """재확인 조건을 지금 root(작업 트리 또는 worktree)에서 실행. bool. 형식 오류는 ValueError."""
    if not isinstance(recheck, dict) or not recheck.get("query"):
        raise ValueError("재확인 조건 없음")
    typ = recheck.get("type")
    q = str(recheck["query"]).strip()
    if "\n" in q:
        raise ValueError("재확인 조건은 한 줄")
    if typ == "grep":
        try:
            pat = re.compile(q)
        except re.error:
            raise ValueError("정규식 오류")
        glob = recheck.get("path")
        hit = False
        for rel in detect.source_files(root) if not glob else [f for f in _all_files(root) if fnmatch.fnmatch(f, glob)]:
            text = detect._read(root, rel)
            if text and pat.search(text):
                hit = True
                break
        return hit if recheck.get("expect", "match") == "match" else not hit
    if typ == "ast":
        return _run_ast(root, q)
    raise ValueError("재확인 조건 종류 오류: %s" % typ)


def _all_files(root):
    from vmc_harness.core.gitutil import git
    rc, out, _ = git(root, "ls-files", "-z")
    return [f.replace("\\", "/") for f in out.split("\0") if f] if rc == 0 else []


def _run_ast(root, q):
    parts = q.split()
    op = parts[0] if parts else ""
    if op == "unreferenced" and len(parts) == 2:
        p, sym = _split_anchor(parts[1])
        if not sym:
            raise ValueError("심볼 없음")
        if p.endswith(detect.PY_EXT):
            return detect.py_unreferenced_symbol(root, p, sym)
        span = detect.symbol_lines(root, p, sym)
        if not span:
            return False
        if detect.word_refs(root, sym.split(".")[-1], exclude=(p, span[0], span[1])):
            return False
        ts = detect.ts_scan(root, [p] + [f for f in detect.source_files(root, detect.TS_EXT) if f != p])
        if ts and not ts.get("error"):
            return any(u["path"] == p and u.get("symbol") == sym.split(".")[-1] for u in ts.get("unreferenced") or [])
        return True
    if op == "unreferenced_file" and len(parts) == 2:
        p = parts[1]
        if not (Path(root) / p).is_file():
            return False
        if p.endswith(detect.PY_EXT):
            return detect.py_unreferenced_file(root, p)
        if detect.TS_ENTRY_RE.search(p) or detect.is_test_path(p):
            return False
        stem = Path(p).name.split(".")[0]
        return not [r for r in detect.word_refs(root, stem) if r[0] != p]
    if op == "dup" and len(parts) == 3:
        a, b = _split_anchor(parts[1]), _split_anchor(parts[2])
        ha = detect.symbol_norm_hash(root, a[0], a[1])
        hb = detect.symbol_norm_hash(root, b[0], b[1])
        return bool(ha) and ha == hb
    if op in ("long_function", "long_file") and len(parts) == 4 and parts[2] == ">":
        p, sym = _split_anchor(parts[1])
        span = detect.symbol_lines(root, p, sym if op == "long_function" else None)
        return bool(span) and (span[1] - span[0] + 1) > int(parts[3])
    if op == "defined" and len(parts) == 2:
        p, sym = _split_anchor(parts[1])
        return detect.symbol_lines(root, p, sym) is not None
    raise ValueError("ast 질의 형식 오류: %s" % q)


# ---- 받기 ----

def target_hash(root, anchor):
    src = detect.symbol_source(root, anchor.get("path"), anchor.get("symbol"))
    return ledger.sha(src) if src is not None else None


def accept(root, proposal, check_root=None):
    """(후보 기록 | None, 거부 이유). 재확인 조건이 지금 참일 때만 장부에 넣는다.
    check_root: 재확인·해시를 계산할 트리(루프는 최신 main worktree). 장부는 root에."""
    at = check_root or root
    if not isinstance(proposal, dict):
        return None, "형식 오류"
    change = proposal.get("change")
    if change not in CHANGES:
        return None, "change 종류 오류"
    anchor = proposal.get("anchor") or {}
    rel = str(anchor.get("path") or "").replace("\\", "/")
    if not rel or re.search(r":\d+$", rel):
        return None, "앵커 없음(경로+심볼, 줄 번호 아님)"
    anchor = {"path": rel, "symbol": anchor.get("symbol") or None}
    if not str(proposal.get("summary") or "").strip():
        return None, "요약 없음"
    if is_protected(root, rel):
        return None, "보호 영역"
    try:
        if not run_recheck(at, proposal.get("recheck")):
            return None, "재확인 조건 거짓"
    except ValueError as e:
        return None, str(e)
    if change in DIRECT and change != "add_pinning_test" and is_write_path(at, rel, anchor["symbol"]):
        change = "write_path"  # 쓰기 경로는 제안만(C)
    for c in ledger.list_candidates(root):
        if c.get("kind") == "cleansing" and c.get("status") == "open" and c.get("anchor") == anchor \
                and c.get("change") == change:
            return ledger.update_candidate(root, c["candidate_id"], count=int(c.get("count") or 1) + 1), None
    rec = ledger.create_candidate(root, "cleansing", proposal["summary"], recheck=proposal["recheck"],
                                  anchor=anchor, target_hash=target_hash(at, anchor))
    from vmc_harness.core.gitutil import head
    rec = ledger.update_candidate(root, rec["candidate_id"], change=change, created_commit=head(at),
                                  area=detect.module_of(root, rel), recheck_at=now_iso())
    return rec, None


# ---- 낡음 ----

def refresh(root, cand, at_root):
    """처리 직전 최신 main(at_root = 그 시점 worktree)에서 재확인. 'open' | 'resolved'."""
    try:
        ok = run_recheck(at_root, cand.get("recheck"))
    except ValueError:
        ok = False
    if not ok:
        ledger.update_candidate(root, cand["candidate_id"], status="resolved", resolved_reason="natural",
                                resolved_at=now_iso())
        return "resolved"
    ledger.update_candidate(root, cand["candidate_id"], target_hash=target_hash(at_root, cand.get("anchor") or {}),
                            recheck_at=now_iso())
    return "open"


def busy_files(root, main):
    """작업 중인 영역: 최근 사람 커밋 파일 + 열린 브랜치 파일 + 지금 작업 트리 변경."""
    hours = config.get("loop.recent_human_commit_hours", root)
    return gitops.human_commit_files(root, hours, main) | gitops.open_branch_files(root, main) | gitops.dirty_files(root)


def expire_if_stale(root, cand, main):
    """사람 커밋이 여러 번 쌓인 영역의 후보는 만료. True면 만료됨."""
    path = (cand.get("anchor") or {}).get("path")
    n = gitops.human_commit_count(root, path, cand.get("created_commit"), main)
    if n >= config.get("loop.expire_human_commits", root):
        ledger.update_candidate(root, cand["candidate_id"], status="expired", resolved_at=now_iso())
        return True
    return False


def expire_proposed(root, at=None):
    """제안(proposed)만 하고 아무도 손대지 않은 후보를 loop.proposed_expire_days 뒤 만료. 반환: 만료한 후보 id 목록.
    손댄 시각 = 제안 뒤 마지막 갱신(updated_at·proposed_at 중 늦은 것). 사람이 처리하면 상태가 proposed에서 바뀐다."""
    from vmc_harness.core.clock import now, parse_iso
    import datetime as _dt
    at = at or now()
    limit = _dt.timedelta(days=config.get("loop.proposed_expire_days", root))
    out = []
    for c in ledger.list_candidates(root):
        if not c or c.get("status") != "proposed":
            continue
        touched = [t for t in (parse_iso(c.get("updated_at")), parse_iso(c.get("proposed_at")),
                               parse_iso(c.get("created_at"))) if t]
        if touched and at - max(touched) >= limit:
            ledger.update_candidate(root, c["candidate_id"], status="expired", resolved_reason="proposed_timeout",
                                    resolved_at=at.isoformat(timespec="seconds"))
            out.append(c["candidate_id"])
    return out


# 루프 처리 순서(종류): 안전망 복구(기준선 실패 테스트) → 진단 제안 티켓 → 클렌징. 같은 종류 안에서는 헤맨 영역부터
LOOP_KINDS = ("test_repair", "ticket", "cleansing")


def open_candidates(root, kinds=("cleansing",)):
    return [c for c in ledger.list_candidates(root) if c and c.get("kind") in kinds and c.get("status") == "open"]


def prioritized(root, scores=None):
    """종류 순(LOOP_KINDS) → 헤맨 흔적이 있는 영역부터 → 같은 점수면 오래된 것부터."""
    scores = detect.wander(root) if scores is None else scores
    cands = open_candidates(root, LOOP_KINDS)
    return sorted(cands, key=lambda c: (LOOP_KINDS.index(c.get("kind")),
                                        -detect.wander_score(scores, (c.get("anchor") or {}).get("path") or ""),
                                        c.get("created_at") or ""))


def covered_by_tests(root, anchor):
    """안전망 판정(싸게): 테스트 파일이 이 심볼 이름이나 모듈 이름을 언급하는가. 엄밀한 확인은 fixation이 한다."""
    rel = anchor.get("path") or ""
    names = [Path(rel).stem]
    if anchor.get("symbol"):
        names.append(anchor["symbol"].split(".")[-1])
    tests = [f for f in _all_files(root) if detect.is_test_path(f)]
    for name in names:
        if name and detect.word_refs(root, name, files=tests):
            return True
    return False
