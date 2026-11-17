"""DB 안전장치 훅 (safety 모듈 — 예외면 디스패처가 막는다, fail-closed).

on_pre_tool
- SQL 파일 쓰기는 패치 폴더(`client.patch_dir`) 아래만. DB를 쓰는 프로젝트가 아니면(patch_dir None) 발동 안 함
- 하네스 비밀값 저장소(키체인 vmc-harness 항목·`~/.vmc-harness/secrets/`)와 DB 접속 정보 파일(db.json) 읽기·쓰기 차단
  — 에이전트 손에 들어가면 세션 기록에 평문으로 남는다(P9)
DB 직접 접속 명령 차단은 gates.handlers가 `sqlguard.is_direct_db_command`로 한다(중복 차단 안 함).

on_session_start
- 기계 사실(호환성 수준)이 오래됐으면 백그라운드로 다시 읽는다(P3). 기다리지 않는다.
"""

import re
import sys
from pathlib import Path

from vmc_harness.hooks.contract import HookResult

_SQL = re.compile(r"(?i)\.sql$")
_SHELL_TARGETS = [
    re.compile(r"(?:^|[^<>&0-9])>>?\s*(['\"]?)([^\s'\";|&]+\.sql)\1", re.I),
    re.compile(r"\btee\s+(?:-a\s+)?(['\"]?)([^\s'\";|&]+\.sql)\1", re.I),
    re.compile(r"(?i)\b(?:out-file|set-content|add-content|new-item)\b[^;|&]*?(?:-(?:file)?path\s+)?(['\"]?)([^\s'\";|&]+\.sql)\1"),
    re.compile(r"(?i)\b(?:cp|mv|copy|move|copy-item|move-item|rename-item|ren)\b[^;|&]*\s(['\"]?)([^\s'\";|&]+\.sql)\1\s*(?:$|[;|&])"),
    re.compile(r"(?i)\bsed\s+-i\S*\s+[^;|&]*\s(['\"]?)([^\s'\";|&]+\.sql)\1"),
]
# 키체인 내보내기는 `security export` 명령만 — 옛 `export\s+.*keychain`은 한 줄의 다른 명령(`grep "export type"; grep keychain`)까지 이어 잡았다(현장 결함 C8)
_SECRET_CMD = re.compile(r"(?i)(find-generic-password|dump-keychain|\bsecurity\s+export\b|get-storedcredential|credread"
                         r"|\.vmc-harness[\\/]+secrets|\.vmc-harness[\\/]+projects[\\/]+[^\s'\"]+[\\/]+db(-facts)?\.json)")
_SECRET_PATH = re.compile(r"(?i)\.vmc-harness[\\/]+(secrets([\\/]|$)|projects[\\/]+[^\\/]+[\\/]+db\.json$)")


def _norm(p):
    return str(p).replace("\\", "/")


def _rel_to_root(root, p):
    s = _norm(p)
    try:
        pp = Path(s)
        if pp.is_absolute():
            return pp.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return None  # 프로젝트 밖
    while s.startswith("./"):
        s = s[2:]
    return s


def _under(rel, folder):
    if rel is None or rel.startswith("../"):
        return False
    a, b = rel.lower(), folder.lower().rstrip("/") + "/"
    return a.startswith(b)


def sql_write_violation(root, write_paths, command=None):
    """패치 폴더 밖 SQL 쓰기면 위반 문구, 아니면 None."""
    targets = [p for p in (write_paths or []) if _SQL.search(_norm(p))]
    if command:
        for rx in _SHELL_TARGETS:
            for m in rx.finditer(command):
                targets.append(m.group(2))
    if not targets:
        return None   # SQL 쓰기가 없으면 DB 설정(db.client — 드라이버·기계 사실 모듈)을 읽지 않는다(훅 기동 비용 E2)
    from vmc_harness.db import client
    pdir = client.patch_dir(root)
    if not pdir:
        return None
    for t in targets:
        rel = _rel_to_root(root, t)
        if not _under(rel, pdir):
            return "SQL 파일 쓰기 위치 위반: %s (SQL 파일은 %s/ 아래만)" % (_norm(t), pdir)
    return None


def secret_access_violation(ctx):
    cmd = ctx.command or ""
    if cmd and _SECRET_CMD.search(cmd) and not re.match(r"^\s*vmc-harness\b", cmd):
        return "하네스 비밀값·DB 접속 정보 직접 접근 차단됨"
    ti = ctx.tool_input or {}
    for key in ("file_path", "path", "notebook_path"):
        v = ti.get(key)
        if isinstance(v, str) and _SECRET_PATH.search(_norm(v)):
            return "하네스 비밀값·DB 접속 정보 직접 접근 차단됨"
    for p in ctx.write_paths or []:
        if _SECRET_PATH.search(_norm(p)):
            return "하네스 비밀값·DB 접속 정보 직접 접근 차단됨"
    return None


def on_pre_tool(ctx):
    if not ctx.root:
        return None
    gate, msg = SECRET, secret_access_violation(ctx)
    if not msg:
        gate, msg = SQL_WRITE, sql_write_violation(ctx.root, ctx.write_paths, ctx.command)
    if msg:
        # 사유는 허용 목록 문구로(01 P5) — 꼬리표 없는 사유는 디스패처가 이름 없는 고정 문구로 바꿔 무엇을 어겼는지 사라졌다(현장 결함 C8)
        from vmc_harness.gates.common import block
        from vmc_harness.gates.handlers import blocked
        fallback = "SQL 파일 쓰기 위치 위반" if gate == SQL_WRITE else None
        return blocked(ctx, gate, block(msg, fallback))
    return None


SECRET = "secret"         # 하네스 비밀값·DB 접속 정보 직접 접근(gates.human_only — 에이전트가 풀 수 없다)
SQL_WRITE = "sql_write"   # 패치 폴더 밖 SQL 쓰기


def on_session_start(ctx):
    """편의 경로 — 어떤 오류도 세션을 막지 않게 여기서 삼킨다."""
    if not ctx.root:
        return None
    try:
        from vmc_harness.db import client, facts
        if not client.load_db_json(ctx.root).get("query") or not facts.stale(ctx.root):
            return None
        from vmc_harness.core.proc import spawn_detached
        spawn_detached([sys.executable, "-m", "vmc_harness", "db", "facts", "--refresh"], cwd=str(ctx.root))
    except Exception:
        pass
    return None
