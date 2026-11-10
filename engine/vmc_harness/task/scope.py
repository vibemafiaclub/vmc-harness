"""선언 범위 대조·이번 작업이 건드린 파일·요청 밖 변경 (spec 03 §요청 밖 변경).

요청 밖 변경은 **기록과 표시만** 한다 — 되돌리기·이유 달기를 강제하지 않는다(01 P2).

"이번 작업이 바꾼 것"의 원본은 이 파일 하나다(현장 결함 원인 ③). 변조 게이트·완료 게이트·과설계·범위 비율이
모두 take_base(착수 스냅샷)와 changes()/touched()/existed_at_base()만 쓴다.
- 스냅샷: HEAD + 착수 때 이미 바뀌어 있던 파일의 해시 + 하네스 문서(AGENTS.md·CLAUDE.md)의 하네스 부분을 뺀 해시
  + 같은 세션 앞 작업이 새로 만든 파일(created)
- 착수 전부터 바뀌어 있던 파일은 해시가 그대로면 이번 작업이 아니다. 세션을 다시 열면(resume) 그 사이 바깥 변경도 흡수
- 하네스가 쓴 파일(.harness/**·.mcp.json·.claude/settings.json·.codex/hooks.json, AGENTS.md 하네스 블록·
  CLAUDE.md `@AGENTS.md` 줄)은 항상 뺀다
"""

import fnmatch
import hashlib
import re
from pathlib import Path

from vmc_harness.core import gitutil
from vmc_harness.task import state as st

HARNESS_PREFIX = ".harness/"


def norm(p):
    s = str(p or "").replace("\\", "/").strip()
    while s.startswith("./"):
        s = s[2:]
    return s.rstrip("/") if s not in ("", "/") else s


def _module_of(root, path):
    try:
        from vmc_harness.bootstrap.structmap import module_of
    except ImportError:
        return None
    try:
        return module_of(root, path)
    except Exception:
        return None


def in_scope(root, declare, path):
    """path가 선언 범위(scope·new·test_change.files) 안인가. 경로·디렉터리 접두·glob·모듈 id를 받는다."""
    path = norm(path)
    entries = [norm(x) for x in (declare.get("scope") or []) + (declare.get("new") or [])]
    tc = declare.get("test_change") or {}
    entries += [norm(x) for x in (tc.get("files") or [])]
    if not entries:
        return False
    mod = None
    for e in entries:
        if not e:
            continue
        if e in (".", "*", "**"):
            return True
        if path == e or path.startswith(e + "/"):
            return True
        if any(ch in e for ch in "*?[") and (fnmatch.fnmatch(path, e) or fnmatch.fnmatch(path, e + "/*")):
            return True
    mod = _module_of(root, path)
    if mod:
        m = norm(mod)
        for e in entries:
            if e and (m == e or m.endswith("/" + e) or e.endswith("/" + m)):
                return True
    return False


HARNESS_FILES = (".mcp.json", ".claude/settings.json", ".codex/hooks.json")
HARNESS_DOCS = ("AGENTS.md", "CLAUDE.md")
_IMPORT_LINE = re.compile(r"^[ \t]*@AGENTS\.md[ \t]*$", re.M)


def harness_owned(rel):
    """하네스가 쓰는 파일 — 사람·에이전트 작업으로 세지 않는다."""
    rel = norm(rel)
    return rel.startswith(HARNESS_PREFIX) or rel in HARNESS_FILES


def _doc_hash(root, rel):
    """AGENTS.md는 하네스 표시 블록을, CLAUDE.md는 `@AGENTS.md` 줄을 빼고(줄끝 무시) 해시. 파일 없으면 None."""
    try:
        text = (Path(root) / rel).read_bytes().decode("utf-8", "replace")
    except OSError:
        return None
    text = text.replace("\r\n", "\n")
    if rel == "AGENTS.md":
        from vmc_harness.gates.rules import BEGIN, END
        text = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "", text, flags=re.S)
    else:
        text = _IMPORT_LINE.sub("", text)
    return hashlib.sha1(text.strip().encode("utf-8")).hexdigest()


def take_base(root, created=None):
    """착수 스냅샷: HEAD · 이미 바뀌어 있던 파일 해시 · 하네스 문서의 하네스 부분 뺀 해시 · 앞 작업이 만든 파일."""
    from vmc_harness.core.clock import now_iso
    head = gitutil.head(root)
    dirty = {}
    for f in gitutil.changed_files(root, head) if head else gitutil.changed_files(root):
        dirty[f] = st.file_hash(Path(root) / f)
    docs = {d: _doc_hash(root, d) for d in HARNESS_DOCS}
    return {"head": head, "dirty": dirty, "docs": docs, "created": sorted(set(created or [])), "taken_at": now_iso()}


def _mtime_after(root, rel, since):
    import datetime as _dt
    try:
        m = _dt.datetime.fromtimestamp((Path(root) / rel).stat().st_mtime).astimezone()
    except OSError:
        return True
    return m >= since


def changes(root, snap, edits=(), exclude=()):
    """스냅샷 이후 바뀐 파일(루트 상대, 정렬). 이 함수 하나가 "이번 작업이 바꾼 것"의 원본이다.
    snap: take_base 결과. dirty가 없는 옛·세션 스냅샷({"head", "since"})은 since 이후 수정 시각으로 거른다.
    edits: 훅이 기록한 편집 경로(지금 파일이 있을 때만 — 셸 명령 조각 같은 가짜 경로는 버린다).
    exclude: 다른 진행 중 작업의 편집(그 작업 것)."""
    snap = snap or {}
    head = snap.get("head")
    dirty = snap.get("dirty")
    docs = snap.get("docs") or {}
    since = None
    if dirty is None and snap.get("since"):
        from vmc_harness.core.clock import parse_iso
        since = parse_iso(snap.get("since"))
    dirty = dirty or {}
    edits = [norm(x) for x in edits or []]
    exclude = {norm(x) for x in exclude or []}
    out = {e for e in edits if e and (Path(root) / e).is_file()}
    for f in gitutil.changed_files(root, head) if head else gitutil.changed_files(root):
        f = norm(f)
        if f in exclude and f not in edits:
            continue
        if f in dirty and dirty[f] == st.file_hash(Path(root) / f):
            continue  # 착수 전부터 바뀌어 있었고 그 뒤로 그대로
        if since is not None and f not in edits and not _mtime_after(root, f, since):
            continue
        out.add(f)
    res = []
    for x in sorted(out):
        if not x or harness_owned(x) or is_artifact(x):
            continue
        if x in HARNESS_DOCS and x in docs and docs[x] == _doc_hash(root, x):
            continue  # 하네스 블록·import 줄만 바뀜
        res.append(x)
    return res


def touched(root, task):
    """이번 작업이 건드린 파일(루트 상대) — 착수 이후 바뀐 파일 중 이 작업이 쓴 것(훅이 기록한 편집)만.
    같은 작업 트리를 여러 세션이 함께 쓰면 git 변경에는 다른 세션·끝난 작업·사람이 쓴 파일과 그들의 커밋이 섞인다.
    그래서 git 변경만으로는 이 작업 것으로 세지 않는다. 해석 못 한 셸 명령의 쓰기는 실행 뒤 대조가 편집으로 넣는다(record_writes)."""
    edits = task.get("edits") or []
    mine = {norm(x) for x in edits}
    return [f for f in changes(root, task.get("base") or {}, edits=edits) if f in mine]


def written_since(root, since_ns, head=None):
    """since_ns(time.time_ns) 이후 수정된 작업 트리 변경 파일 — 해석 못 한 셸 명령이 쓴 파일 찾기.
    지워진 파일·하네스 파일·도구 산출물은 뺀다. 그 사이 다른 세션이 쓴 파일이 섞일 수 있다(실행 시간 창 안만)."""
    out = []
    for f in gitutil.changed_files(root, head) if head else gitutil.changed_files(root):
        f = norm(f)
        if not f or harness_owned(f) or is_artifact(f):
            continue
        try:
            if (Path(root) / f).stat().st_mtime_ns >= since_ns:
                out.append(f)
        except OSError:
            continue
    return out


def record_writes(task, rels):
    """편집 목록에 경로를 더한다(이미 있으면 그대로). 더한 게 있으면 True. 저장은 호출자가."""
    edits = task.setdefault("edits", [])
    added = False
    for r in rels:
        r = norm(r)
        if r and r not in edits:
            edits.append(r)
            added = True
    return added


task_changes = touched


def existed_at_base(root, snap, rel, created=()):
    """rel이 스냅샷 때 이미 있던 파일인가(이번 세션·작업이 새로 만든 파일이 아닌가). 검증물 변조 판정의 "기존"."""
    rel = norm(rel)
    snap = snap or {}
    if rel in set(snap.get("created") or []) or rel in set(created or []):
        return False
    dirty = snap.get("dirty") or {}
    if rel in dirty:
        return dirty[rel] is not None
    head = snap.get("head")
    if not head:
        return False
    rc, _, _ = gitutil.git(root, "cat-file", "-e", "%s:%s" % (head, rel))
    return rc == 0


def absorb_outside_changes(root, task):
    """세션을 다시 열 때(resume·재개): 그 사이 이 작업 밖에서 생긴 미커밋 변경을 스냅샷에 넣는다(해시가 그대로면 제외)."""
    base = task.get("base")
    if not isinstance(base, dict) or "dirty" not in base:
        return task
    mine = {norm(x) for x in task.get("edits") or []} | set(task.get("created") or [])
    cache = set((task.get("scope_cache") or {}).get("files") or [])
    dirty = base.setdefault("dirty", {})
    for f in gitutil.changed_files(root, base.get("head")) if base.get("head") else gitutil.changed_files(root):
        f = norm(f)
        if f in dirty or f in mine or f in cache:
            continue
        dirty[f] = st.file_hash(Path(root) / f)
    return task


# 도구가 만드는 산출물 — .gitignore가 없는 레포에서도 "건드린 파일"(범위 밖·과설계 새 파일)로 세지 않는다
ARTIFACT_DIRS = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules", ".tox", ".next")
ARTIFACT_SUFFIX = (".pyc", ".pyo", ".DS_Store")


def is_artifact(rel):
    parts = rel.split("/")
    return any(p in ARTIFACT_DIRS for p in parts[:-1]) or rel.endswith(ARTIFACT_SUFFIX)


def numstat(root, task, files=None):
    """{path: (added, removed)} — 착수 기준 대비. 미추적 파일은 줄 수 전체를 추가로."""
    base = (task.get("base") or {}).get("head")
    files = files if files is not None else touched(root, task)
    out = {}
    if not files:
        return out
    args = ["diff", "--numstat", "--no-renames", "-z", base or "HEAD", "--"] + list(files)
    rc, text, _ = gitutil.git(root, *args)
    if rc == 0:
        for item in gitutil.split_z(text):
            parts = item.split("\t")
            if len(parts) == 3:
                a, r, p = parts
                out[norm(p.strip("\n"))] = (int(a) if a.isdigit() else 0, int(r) if r.isdigit() else 0)
    rc, tracked_text, _ = gitutil.git(root, "ls-files", "-z", "--", *files)
    tracked = {norm(l) for l in gitutil.split_z(tracked_text)} if rc == 0 else set()
    for f in files:
        if f in out:
            continue
        p = Path(root) / f
        if f in tracked:
            out[f] = (0, 0)   # 추적 중인데 diff가 없다 = 편집 시도만 있고 내용은 그대로
        elif p.is_file():
            try:
                n = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                n = 0
            out[f] = (n, 0)
        else:
            out[f] = (0, 0)
    return out


def added_lines(root, task, files=None):
    return sum(a for a, _ in numstat(root, task, files).values())


def out_of_scope(root, task, files=None):
    """[{"path","reason","added","removed"}] — reason은 에이전트가 남긴 한 줄(없으면 None)."""
    declare = task.get("declare") or {}
    files = files if files is not None else touched(root, task)
    reasons = task.get("out_of_scope_reasons") or {}
    outside = [f for f in files if not in_scope(root, declare, f)]
    stats = numstat(root, task, outside) if outside else {}
    return [{"path": f, "reason": reasons.get(f), "added": stats.get(f, (0, 0))[0],
             "removed": stats.get(f, (0, 0))[1]} for f in outside]


def touch_ratio(root, task, files=None):
    """선언 대비 실제 건드린 비율(참고 지표): (범위 안 편집 비율, 선언한 파일 중 실제로 건드린 비율)."""
    declare = task.get("declare") or {}
    files = files if files is not None else touched(root, task)
    if not files:
        return {"in_scope": None, "declared_used": None}
    inside = [f for f in files if in_scope(root, declare, f)]
    declared_files = [norm(x) for x in (declare.get("scope") or []) + (declare.get("new") or [])
                      if Path(norm(x)).suffix and not any(ch in x for ch in "*?[")]
    used = [d for d in declared_files if d in files]
    return {"in_scope": round(len(inside) / float(len(files)), 3),
            "declared_used": round(len(used) / float(len(declared_files)), 3) if declared_files else None}
