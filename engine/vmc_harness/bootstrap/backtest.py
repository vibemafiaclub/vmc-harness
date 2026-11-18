"""백테스트 실행기 (spec 07) — 후보 게이트 조건을 지난 커밋(git log)·세션 도구 호출에 돌려 "막았을 횟수·실행 시간".

- 게이트 조건에 **그대로 걸리는 것만** 센다 → 결과는 "최소 N건"(보수적)
- "문제가 된 변경"(이후 고쳐지거나 되돌려진 것)은 오탐이 있어 finding이 아니라 제안 순위 근거로만 쓴다

조건(dict, type별):
  path_write    {"globs":[...]}                        경로 glob에 쓰기(커밋 변경 파일·세션 쓰기 도구)
  write_outside {"ext":[".sql"], "allowed":[glob...]}   그 확장자 파일을 허용 폴더 밖에 쓰기
  command       {"regex":"..."}                         세션 셸 명령 정규식
  db_direct     {}                                      DB 직접 접속 명령(db 영역 sqlguard가 있으면 그것)
  test_modify   {"globs":[...]?}                        기존 테스트 파일 수정·삭제(새 테스트 추가는 제외)
  diff_regex    {"regex":"...", "globs":[...]?}         커밋에서 추가된 줄 정규식(예 .sql에 OPENJSON)
"""

import json
import re
import time
from datetime import timedelta
from pathlib import PurePosixPath

from vmc_harness.bootstrap import signals, util
from vmc_harness.core import config, gitutil
from vmc_harness.core.clock import parse_iso
from vmc_harness.core.mask import mask

TYPES = ("path_write", "write_outside", "command", "db_direct", "test_modify", "diff_regex")
FIX_MSG = re.compile(r"(?i)(\bfix|\brevert|hotfix|bug|수정|버그|원복|되돌|롤백|오류)")
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]*\.py$|_test\.py$|\.(test|spec)\.[jt]sx?$")
_DB_DIRECT = re.compile(
    r"(?i)(\bsqlcmd\b|\bosql\b|\bbcp\s|Invoke-Sqlcmd|pyodbc\.connect|pymssql\.connect|"
    r"create_engine\(\s*['\"]mssql|mssql\+pyodbc://|Driver=\{?ODBC Driver|Server=[^;\s]+;\s*Database=|New-Object\s+System\.Data\.SqlClient)")
_FAIL = re.compile(r"(Traceback|Exception|Error[:\s]|에러|오류|안\s?돼|안\s?됨|작동\s?안|동작\s?안|깨져|깨짐)", re.I)


def is_db_direct(command):
    st, res = util.optional_call([("vmc_harness.db.sqlguard", "is_direct_db_command")], command)
    if st == "ok":
        return bool(res)
    return bool(_DB_DIRECT.search(command or ""))


def _commits(root, max_commits, with_patch=False, globs=None):
    """[{sha, at, subject, files:[(status, path)], added_lines:{path:[...]}}]"""
    args = ["log", "-n", str(max_commits), "--format=%x1e%H%x1f%cI%x1f%s", "--no-renames"]
    args += ["-p", "-U0", "--no-color"] if with_patch else ["--name-status"]
    rc, out, _ = gitutil.git(root, *args, timeout=300)
    if rc != 0:
        return []
    commits = []
    for chunk in out.split("\x1e"):
        if not chunk.strip():
            continue
        head, _, body = chunk.partition("\n")
        parts = head.split("\x1f")
        if len(parts) < 3:
            continue
        c = {"sha": parts[0], "at": parts[1], "subject": parts[2], "files": [], "added": {}}
        if with_patch:
            cur = None
            for line in body.splitlines():
                if line.startswith("diff --git "):
                    m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
                    cur = m.group(2) if m else None
                    if cur:
                        c["files"].append(("M", cur))
                elif line.startswith("new file mode") and c["files"]:
                    c["files"][-1] = ("A", c["files"][-1][1])
                elif line.startswith("deleted file mode") and c["files"]:
                    c["files"][-1] = ("D", c["files"][-1][1])
                elif cur and line.startswith("+") and not line.startswith("+++"):
                    c["added"].setdefault(cur, []).append(line[1:])
        else:
            for line in body.splitlines():
                bits = line.split("\t")
                if len(bits) >= 2 and bits[0][:1] in "AMDTC":
                    c["files"].append((bits[0][:1], bits[-1]))
        commits.append(c)
    return commits


def _match_write_path(cond, path, status=None, existed=True):
    t = cond["type"]
    if t == "path_write":
        return util.any_glob(path, cond.get("globs"))
    if t == "write_outside":
        exts = [e.lower() for e in cond.get("ext") or []]
        if exts and PurePosixPath(path).suffix.lower() not in exts:
            return False
        return not util.any_glob(path, cond.get("allowed"))
    if t == "test_modify":
        if not TEST_PATH.search(path):
            return False
        if cond.get("globs") and not util.any_glob(path, cond["globs"]):
            return False
        return status in ("M", "D") or (status is None and existed)
    return False


def _within(a, b, days):
    da, db = parse_iso(a), parse_iso(b)
    if not da or not db:
        return False
    return timedelta(0) < (db - da) <= timedelta(days=days)


def run(root, condition, sessions=None, max_scenes=None):
    """조건 하나 백테스트. 결과 dict(맨 위 docstring)."""
    t0 = time.time()
    cond = dict(condition or {})
    t = cond.get("type")
    if t not in TYPES:
        raise ValueError("백테스트 조건 종류 오류: %s (가능: %s)" % (t, ", ".join(TYPES)))
    max_commits = config.get("backtest.max_commits", root)
    max_scenes = max_scenes or config.get("backtest.max_scenes", root)
    window = config.get("backtest.fix_window_days", root)
    regex = re.compile(cond["regex"], re.I | re.M) if cond.get("regex") else None
    scenes = []
    commit_hits = []

    # ---- 커밋
    commits = []
    if t in ("path_write", "write_outside", "test_modify", "diff_regex"):
        commits = _commits(root, max_commits, with_patch=(t == "diff_regex"))
        for c in commits:
            for status, path in c["files"]:
                hit = False
                if t == "diff_regex":
                    if cond.get("globs") and not util.any_glob(path, cond["globs"]):
                        continue
                    lines = [l for l in c["added"].get(path, []) if regex.search(l)]
                    hit = bool(lines)
                    detail = (lines[0].strip()[:120] if lines else "")
                else:
                    hit = _match_write_path(cond, path, status)
                    detail = status
                if hit:
                    commit_hits.append({"sha": c["sha"], "at": c["at"], "path": path})
                    scenes.append({"source": "commit", "at": c["at"], "ref": c["sha"][:8], "agent": None,
                                   "match": path, "detail": mask(detail or "")})
    # 문제가 된 변경: 같은 파일을 기간 안에 고친·되돌린 뒤 커밋
    problem = 0
    if commit_hits and commits:
        pos = {c["sha"]: i for i, c in enumerate(commits)}  # git log 순서: 최신이 앞
        for h in commit_hits:
            for c in commits[:pos[h["sha"]]]:
                da, db = parse_iso(h["at"]), parse_iso(c["at"])
                if not da or not db or (db - da) > timedelta(days=window):
                    continue
                if FIX_MSG.search(c["subject"]) and any(p == h["path"] for _, p in c["files"]):
                    problem += 1
                    break

    # ---- 세션 도구 호출
    sess_events, sess_ids = 0, set()
    tool_calls = 0
    tracked = set()
    if t == "test_modify":
        rc, out, _ = gitutil.git(root, "ls-files")
        tracked = set(out.splitlines()) if rc == 0 else set()
    path_hits = []
    for s in sessions or []:
        for tc in s.get("tool_calls") or []:
            tool_calls += 1
            matched = None
            if t in ("path_write", "write_outside", "test_modify") and tc.get("write"):
                for p in tc.get("paths") or []:
                    if _match_write_path(cond, p, None, existed=(p in tracked) if t == "test_modify" else True):
                        matched = p
                        break
            elif t == "command":
                for cmd in tc.get("commands") or []:
                    if signals.cmd_search(regex, cmd):
                        matched = cmd
                        break
            elif t == "db_direct":
                for cmd in tc.get("commands") or []:
                    if is_db_direct(cmd):
                        matched = cmd
                        break
            if matched:
                sess_events += 1
                sess_ids.add(s.get("session_id"))
                if t in ("path_write", "write_outside", "test_modify"):
                    path_hits.append((tc.get("at") or s.get("started_at"), s.get("session_id"), matched))
                scenes.append({"source": "session", "at": tc.get("at") or s.get("started_at"),
                               "ref": (s.get("session_id") or "")[:8], "agent": s.get("agent"),
                               "match": util.scrub_user(mask(matched))[:200], "detail": tc.get("name")})
    # 세션 쪽 "문제가 된 변경": 다른 뒤 세션이 같은 파일을 다시 고쳤고 그 세션 사람 프롬프트에 오류 신호
    if path_hits and sessions:
        later = sorted((s for s in sessions if s.get("kind", "main") == "main"), key=lambda s: s.get("started_at") or "")
        for at, sid, p in path_hits:
            for s in later:
                if s.get("session_id") == sid or not _within(at, s.get("started_at"), window):
                    continue
                if any(p in (tc.get("paths") or []) for tc in s.get("tool_calls") or [] if tc.get("write")) and \
                        any(_FAIL.search(x.get("text") or "") for x in s.get("human_prompts") or []):
                    problem += 1
                    break

    scenes.sort(key=lambda x: x.get("at") or "")
    total = len(commit_hits) + sess_events
    return {
        "condition": cond,
        "would_block": total,
        "commits": len({h["sha"] for h in commit_hits}),
        "commit_files": len(commit_hits),
        "session_events": sess_events,
        "sessions": len(sess_ids),
        "problem_changes": problem,
        "scenes": scenes[:max_scenes],
        "scanned": {"commits": len(commits), "sessions": len(sessions or []), "tool_calls": tool_calls},
        "elapsed_sec": round(time.time() - t0, 2),
        "note": "최소 %d건 — 조건에 그대로 걸린 것만 셌다" % total,
    }


def condition_from_args(args):
    if getattr(args, "condition", None):
        return json.loads(args.condition)
    if args.db_direct:
        return {"type": "db_direct"}
    if args.command_regex:
        return {"type": "command", "regex": args.command_regex}
    if args.diff:
        return {"type": "diff_regex", "regex": args.diff, "globs": args.path or []}
    if args.test_modify:
        return {"type": "test_modify", "globs": args.path or []}
    if args.outside:
        return {"type": "write_outside", "ext": args.ext or [], "allowed": args.outside}
    if args.path:
        return {"type": "path_write", "globs": args.path}
    raise ValueError("조건 없음")
