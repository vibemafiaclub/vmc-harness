"""SQL 판정 (spec 02 §DB 안전장치) — MSSQL(T-SQL) 기준.

- `is_select_only(sql) -> (bool, reason)`: 조회 도구가 받는 SQL은 SELECT 하나뿐. 토크나이저로 주석(`--`, 중첩 `/* */`),
  문자열(`'…'`, `N'…'`), 대괄호·큰따옴표 식별자를 걷어 낸 뒤 키워드로 판정한다. 해석할 수 없으면 거부(fail-closed)
- `apply_auto_top(sql, n) -> (sql, how)`: 바깥 SELECT에 `TOP (n)`. 이미 TOP/OFFSET이 있으면 그대로
- `is_direct_db_command(command, cwd=None) -> bool` / `direct_db_reason(...)`: 셸 명령이 DB에 직접 붙는지
- `direct_db_verdict(command, cwd=None) -> (사유, 종류) | None`: 종류 `exec`(DB 도구·드라이버 코드·스크립트 파일을 실행) /
  `text`(실행하지 않는 문자열에만 흔적 — 해석 실패한 명령 원문·데이터 heredoc·일반 명령의 따옴표 인자·JSON 페이로드).
  둘 다 막지만 `text`는 사람이 "그냥 진행"으로 풀 수 있다(gates.proceed_allowed의 db_text)
- 실행하는 스크립트 파일 검사: python(.py·-m 모듈), node·deno·bun·tsx(.js·.mjs·.cjs·.ts·.mts·.cts·.jsx·.tsx),
  PowerShell(.ps1·.psm1), sh·bash·zsh(.sh), cmd(.bat·.cmd)
- `split_batches(text)`: `GO` 배치 구분(주석·문자열 안의 GO는 무시, `GO n` 반복)
- `dryrun_blockers(sql)`: 트랜잭션 롤백으로 되돌릴 수 없는 문장
- `masked_sql(sql)`: 기록용 — 문자열 리터럴을 `'?'`로, 비밀값 패턴 마스킹
"""

import os
import re
import shlex
from pathlib import Path

from vmc_harness.core import config


class SqlParseError(ValueError):
    pass


# ---------------------------------------------------------------- 토크나이저

WORD, QIDENT, STRING, NUMBER, OP, SEMI = "word", "qident", "string", "number", "op", "semi"


class Tok(object):
    __slots__ = ("kind", "text", "start", "end")

    def __init__(self, kind, text, start, end):
        self.kind, self.text, self.start, self.end = kind, text, start, end

    @property
    def up(self):
        return self.text.upper() if self.kind == WORD else ""

    def __repr__(self):
        return "Tok(%s,%r)" % (self.kind, self.text)


def _is_word_start(ch):
    return ch.isalpha() or ch in "_@#"


def _is_word_char(ch):
    return ch.isalnum() or ch in "_@#$"


def tokenize(sql):
    """주석을 건너뛴 토큰 목록. 닫히지 않은 문자열·주석·식별자는 SqlParseError."""
    toks, i, n = [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j + 1
            continue
        if sql.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if sql.startswith("/*", j):
                    depth += 1
                    j += 2
                elif sql.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            if depth:
                raise SqlParseError("닫히지 않은 주석")
            i = j
            continue
        if ch in "nN" and i + 1 < n and sql[i + 1] == "'":
            j = _scan_quoted(sql, i + 1, "'")
            toks.append(Tok(STRING, sql[i:j], i, j))
            i = j
            continue
        if ch == "'":
            j = _scan_quoted(sql, i, "'")
            toks.append(Tok(STRING, sql[i:j], i, j))
            i = j
            continue
        if ch == "[":
            j = _scan_quoted(sql, i, "]")
            toks.append(Tok(QIDENT, sql[i:j], i, j))
            i = j
            continue
        if ch == '"':
            j = _scan_quoted(sql, i, '"')
            toks.append(Tok(QIDENT, sql[i:j], i, j))
            i = j
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and sql[i + 1].isdigit()):
            j = i + 1
            while j < n and (sql[j].isalnum() or sql[j] == "."):
                j += 1
            toks.append(Tok(NUMBER, sql[i:j], i, j))
            i = j
            continue
        if _is_word_start(ch):
            j = i + 1
            while j < n and _is_word_char(sql[j]):
                j += 1
            toks.append(Tok(WORD, sql[i:j], i, j))
            i = j
            continue
        if ch == ";":
            toks.append(Tok(SEMI, ";", i, i + 1))
            i += 1
            continue
        toks.append(Tok(OP, ch, i, i + 1))
        i += 1
    return toks


def _scan_quoted(sql, i, close):
    """sql[i]가 여는 따옴표/대괄호. 닫는 문자 두 번은 이스케이프. 닫힌 다음 위치."""
    j, n = i + 1, len(sql)
    while j < n:
        if sql[j] == close:
            if j + 1 < n and sql[j + 1] == close:
                j += 2
                continue
            return j + 1
        j += 1
    raise SqlParseError("닫히지 않은 문자열 또는 식별자")


# ---------------------------------------------------------------- SELECT 판정

# 조회에서 받지 않는 키워드(쓰기·DDL·실행·권한·트랜잭션·세션 변경·지연·외부 데이터)
FORBIDDEN = {
    "INSERT": "쓰기문(INSERT)", "UPDATE": "쓰기문(UPDATE)", "DELETE": "쓰기문(DELETE)", "MERGE": "쓰기문(MERGE)",
    "UPSERT": "쓰기문", "TRUNCATE": "쓰기문(TRUNCATE)", "INTO": "SELECT INTO·INSERT INTO",
    "CREATE": "DDL(CREATE)", "ALTER": "DDL(ALTER)", "DROP": "DDL(DROP)", "RENAME": "DDL",
    "EXEC": "프로시저 실행(EXEC)", "EXECUTE": "프로시저 실행(EXECUTE)",
    "GRANT": "권한 변경", "REVOKE": "권한 변경", "DENY": "권한 변경",
    "BACKUP": "관리 명령", "RESTORE": "관리 명령", "DBCC": "관리 명령", "KILL": "관리 명령",
    "SHUTDOWN": "관리 명령", "RECONFIGURE": "관리 명령", "CHECKPOINT": "관리 명령", "SETUSER": "세션 변경",
    "USE": "DB 전환(USE)", "SET": "세션 설정(SET)", "DECLARE": "변수 선언", "GO": "배치 구분(GO)",
    "BEGIN": "트랜잭션·블록", "COMMIT": "트랜잭션", "ROLLBACK": "트랜잭션", "SAVE": "트랜잭션",
    "WAITFOR": "지연(WAITFOR)", "BULK": "외부 데이터(BULK)", "OPENROWSET": "외부 데이터(OPENROWSET)",
    "OPENDATASOURCE": "외부 데이터(OPENDATASOURCE)", "OPENQUERY": "연결된 서버(OPENQUERY)",
    "READTEXT": "텍스트 명령", "WRITETEXT": "쓰기문", "UPDATETEXT": "쓰기문",
    "PRINT": "조회 외 문장", "RAISERROR": "조회 외 문장", "THROW": "조회 외 문장", "REVERT": "세션 변경",
    "ENABLE": "DDL", "DISABLE": "DDL", "RECEIVE": "Service Broker", "SEND": "Service Broker",
    "IF": "제어문", "WHILE": "제어문", "GOTO": "제어문", "RETURN": "제어문",
}
SET_OPS = ("UNION", "EXCEPT", "INTERSECT")


def _has_forbidden(toks):
    for idx, t in enumerate(toks):
        if t.kind != WORD:
            continue
        u = t.up
        if u in FORBIDDEN:
            return "%s 불가" % FORBIDDEN[u]
        if u.startswith("XP_") or u.startswith("SP_"):
            return "확장·시스템 프로시저(%s) 불가" % t.text
        if u == "NEXT" and _word_at(toks, idx + 1) == "VALUE" and _word_at(toks, idx + 2) == "FOR":
            return "시퀀스 증가(NEXT VALUE FOR) 불가"
    return None


def _word_at(toks, i):
    return toks[i].up if 0 <= i < len(toks) and toks[i].kind == WORD else ""


def _linked_server(toks):
    """a.b.c.d (점 3개 이상으로 이어진 이름, 빈 부분 `a..c` 포함) — 연결된 서버 참조."""
    run_dots, prev = 0, None
    for t in toks:
        if t.kind in (WORD, QIDENT):
            if prev == "name":
                run_dots = 0  # 이름 뒤에 점 없이 이름(별칭 등) — 새 이름
            prev = "name"
            continue
        if t.kind == OP and t.text == ".":
            if prev is None:
                run_dots = 0
            run_dots += 1
            prev = "."
            if run_dots >= 3:
                return True
            continue
        run_dots, prev = 0, None
    return False


def _skip_parens(toks, i):
    """toks[i] == '(' → 짝 ')' 다음 위치. 짝이 없으면 SqlParseError."""
    depth = 0
    while i < len(toks):
        t = toks[i]
        if t.kind == OP and t.text == "(":
            depth += 1
        elif t.kind == OP and t.text == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise SqlParseError("괄호 짝이 맞지 않음")


def _main_select_index(toks):
    """CTE 머리를 건너뛴 바깥 SELECT(또는 '(') 위치. 형식이 틀리면 SqlParseError."""
    i = 0
    if _word_at(toks, 0) != "WITH":
        return 0
    i = 1
    while True:
        if i >= len(toks) or toks[i].kind not in (WORD, QIDENT):
            raise SqlParseError("CTE 이름 없음")
        i += 1
        if i < len(toks) and toks[i].kind == OP and toks[i].text == "(":
            i = _skip_parens(toks, i)
        if _word_at(toks, i) != "AS":
            raise SqlParseError("CTE 형식 오류")
        i += 1
        if not (i < len(toks) and toks[i].kind == OP and toks[i].text == "("):
            raise SqlParseError("CTE 형식 오류")
        i = _skip_parens(toks, i)
        if i < len(toks) and toks[i].kind == OP and toks[i].text == ",":
            i += 1
            continue
        return i


def _strip_semis(toks):
    while toks and toks[0].kind == SEMI:
        toks = toks[1:]
    while toks and toks[-1].kind == SEMI:
        toks = toks[:-1]
    return toks


def is_select_only(sql):
    """(True, "") 또는 (False, 무엇을 어겼는지)."""
    if not sql or not sql.strip():
        return False, "빈 SQL"
    try:
        toks = _strip_semis(tokenize(sql))
    except SqlParseError as e:
        return False, "SQL 해석 불가: %s" % e
    if not toks:
        return False, "빈 SQL"
    if any(t.kind == SEMI for t in toks):
        return False, "여러 문장 불가"
    bad = _has_forbidden(toks)
    if bad:
        return False, bad
    if _linked_server(toks):
        return False, "연결된 서버(네 부분 이름) 참조 불가"
    try:
        start = _main_select_index(toks)
    except SqlParseError as e:
        return False, "SQL 해석 불가: %s" % e
    if start >= len(toks):
        return False, "SELECT 없음"
    first = toks[start]
    k = start
    while k < len(toks) and toks[k].kind == OP and toks[k].text == "(":
        k += 1
    if _word_at(toks, k) != "SELECT":
        return False, "SELECT 문만 허용(%s)" % (toks[0].text if toks[0].kind == WORD else first.text)
    # 바깥 깊이에서 SELECT가 집합 연산 없이 또 나오면 여러 문장(세미콜론 없는 T-SQL)
    depth, prev = 0, None
    seen_main = False
    for idx in range(start, len(toks)):
        t = toks[idx]
        if t.kind == OP and t.text == "(":
            depth += 1
        elif t.kind == OP and t.text == ")":
            depth -= 1
            if depth < 0:
                return False, "SQL 해석 불가: 괄호 짝이 맞지 않음"
        elif t.kind == WORD and depth == 0:
            if t.up == "SELECT":
                if seen_main and not (prev is not None and prev.kind == WORD and prev.up in SET_OPS + ("ALL",)):
                    return False, "여러 문장 불가"
                seen_main = True
            elif t.up == "WITH" and idx + 2 < len(toks) and toks[idx + 1].kind in (WORD, QIDENT) \
                    and (_word_at(toks, idx + 2) == "AS" or (toks[idx + 2].kind == OP and toks[idx + 2].text == "(")) \
                    and toks[idx + 1].up not in ("TIES", "ROLLUP", "CUBE"):
                return False, "여러 문장 불가"
        prev = t
    if depth != 0:
        return False, "SQL 해석 불가: 괄호 짝이 맞지 않음"
    return True, ""


def apply_auto_top(sql, n):
    """(새 SQL, how). how: "top"(TOP을 넣음) | "kept"(이미 TOP/OFFSET) | "rowcount"(집합 연산 등 — 호출자가 SET ROWCOUNT로 제한).
    is_select_only를 통과한 SQL만 넣는다."""
    toks = _strip_semis(tokenize(sql))
    start = _main_select_index(toks)
    if toks[start].kind == OP:  # (SELECT …) UNION …
        return sql, "rowcount"
    depth = 0
    for t in toks[start:]:
        if t.kind == OP and t.text == "(":
            depth += 1
        elif t.kind == OP and t.text == ")":
            depth -= 1
        elif depth == 0 and t.kind == WORD and t.up in SET_OPS:
            return sql, "rowcount"
        elif depth == 0 and t.kind == WORD and t.up == "OFFSET":
            return sql, "kept"
    i = start + 1
    if _word_at(toks, i) in ("DISTINCT", "ALL"):
        i += 1
    if _word_at(toks, i) == "TOP":
        return sql, "kept"
    pos = toks[i - 1].end
    return sql[:pos] + " TOP (%d)" % int(n) + sql[pos:], "top"


# ---------------------------------------------------------------- 기록용 마스킹

def masked_sql(sql):
    from vmc_harness.core.mask import mask
    try:
        toks = tokenize(sql or "")
    except SqlParseError:
        return "<해석 불가 SQL — 원문 기록 안 함>"
    out, last = [], 0
    for t in toks:
        if t.kind == STRING:
            out.append(sql[last:t.start])
            out.append("'?'")
            last = t.end
    out.append(sql[last:])
    return mask("".join(out))


# ---------------------------------------------------------------- GO 배치

_GO_LINE = re.compile(r"^\s*GO(?:\s+(\d+))?\s*(?:--.*)?$", re.I)
_USE_LINE = re.compile(r"^\s*USE\s+(\[[^\]]+\]|\w+)\s*;?\s*$", re.I)


def split_batches(text):
    """[(batch_sql, start_line(1부터), repeat)] — GO 줄로 나눈다. 블록 주석·문자열 안의 GO는 구분자가 아니다."""
    batches, cur, cur_start = [], [], 1
    in_block, in_str = 0, None  # 블록 주석 깊이, 열린 따옴표 문자
    for lineno, line in enumerate(text.splitlines(), 1):
        if not in_block and not in_str:
            m = _GO_LINE.match(line)
            if m:
                batches.append(("\n".join(cur), cur_start, int(m.group(1) or 1)))
                cur, cur_start = [], lineno + 1
                continue
        cur.append(line)
        in_block, in_str = _line_state(line, in_block, in_str)
    batches.append(("\n".join(cur), cur_start, 1))
    out = []
    for body, start, rep in batches:
        try:
            if not _strip_semis(tokenize(body)):
                continue
        except SqlParseError:
            pass  # 실행 단계에서 서버가 오류를 낸다
        out.append((body, start, rep))
    return out


def _line_state(line, in_block, in_str):
    i, n = 0, len(line)
    while i < n:
        if in_str:
            if line[i] == in_str:
                if i + 1 < n and line[i + 1] == in_str:
                    i += 2
                    continue
                in_str = None
            i += 1
            continue
        if in_block:
            if line.startswith("/*", i):
                in_block += 1
                i += 2
            elif line.startswith("*/", i):
                in_block -= 1
                i += 2
            else:
                i += 1
            continue
        if line.startswith("--", i):
            break
        if line.startswith("/*", i):
            in_block = 1
            i += 2
            continue
        if line[i] in "'\"":
            in_str = line[i]
        elif line[i] == "[":
            in_str = "]"
        i += 1
    return in_block, in_str


def strip_use_lines(batch):
    """배치에서 `USE [db]` 줄을 뺀다(드라이런은 등록된 DB에서만 돈다). (새 배치, 뺀 줄 목록)"""
    kept, removed = [], []
    for line in batch.splitlines():
        if _USE_LINE.match(line):
            removed.append(line.strip())
        else:
            kept.append(line)
    return "\n".join(kept), removed


# ---------------------------------------------------------------- 드라이런 거부 문장

_NON_TX = [
    (("ALTER", "DATABASE"), "ALTER DATABASE"), (("CREATE", "DATABASE"), "CREATE DATABASE"),
    (("DROP", "DATABASE"), "DROP DATABASE"), (("BACKUP",), "BACKUP"), (("RESTORE",), "RESTORE"),
    (("DBCC",), "DBCC"), (("RECONFIGURE",), "RECONFIGURE"), (("SHUTDOWN",), "SHUTDOWN"), (("KILL",), "KILL"),
    (("CREATE", "LOGIN"), "CREATE LOGIN"), (("ALTER", "LOGIN"), "ALTER LOGIN"), (("DROP", "LOGIN"), "DROP LOGIN"),
    (("ALTER", "SERVER"), "ALTER SERVER"), (("CREATE", "ENDPOINT"), "CREATE ENDPOINT"),
    (("CREATE", "FULLTEXT"), "FULLTEXT"), (("ALTER", "FULLTEXT"), "FULLTEXT"), (("DROP", "FULLTEXT"), "FULLTEXT"),
    (("BEGIN", "DISTRIBUTED"), "분산 트랜잭션"), (("OPENROWSET",), "OPENROWSET"), (("OPENDATASOURCE",), "OPENDATASOURCE"),
    (("OPENQUERY",), "OPENQUERY"), (("USE",), "USE(줄 단독이 아닌 DB 전환)"),
]


def dryrun_blockers(sql):
    """트랜잭션 롤백으로 되돌릴 수 없거나 등록 DB 밖에 닿는 문장 목록(빈 목록이면 드라이런 가능)."""
    try:
        toks = tokenize(sql)
    except SqlParseError as e:
        return ["SQL 해석 불가: %s" % e]
    words = [t.up if t.kind == WORD else None for t in toks]
    found = []
    for i in range(len(words)):
        for seq, label in _NON_TX:
            if all(i + k < len(words) and words[i + k] == w for k, w in enumerate(seq)):
                found.append(label)
        w = words[i] or ""
        if w.startswith("XP_"):
            found.append("확장 프로시저(%s)" % toks[i].text)
        if w == "SET" and i + 1 < len(words) and words[i + 1] == "IMPLICIT_TRANSACTIONS":
            found.append("SET IMPLICIT_TRANSACTIONS")
        if w in ("SP_CONFIGURE", "SP_ADDLINKEDSERVER", "SP_ADDSRVROLEMEMBER", "SP_ADDLOGIN"):
            found.append(toks[i].text)
    if _linked_server(toks):
        found.append("연결된 서버(네 부분 이름)")
    # COMMIT이 BEGIN TRAN보다 많으면 드라이런 트랜잭션을 확정해 버린다
    begins = sum(1 for i, w in enumerate(words) if w == "BEGIN" and i + 1 < len(words) and words[i + 1] in ("TRAN", "TRANSACTION"))
    commits = sum(1 for w in words if w == "COMMIT")
    if commits > begins:
        found.append("짝 없는 COMMIT")
    seen, out = set(), []
    for f in found:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


# ---------------------------------------------------------------- 셸 명령: DB 직접 접속

DB_TOOLS = {
    "sqlcmd", "osql", "isql", "tsql", "bcp", "freebcp", "bsqldb", "fisql", "defncopy", "datacopy",
    "mssql-cli", "mssql-scripter", "sqlpackage", "invoke-sqlcmd", "invoke-dbaquery", "usql", "sql-cli",
    "mssql", "sqlcl", "azuredatastudio", "ssms",
}
# 내용을 읽거나 찾기만 하는 도구 — 인자에 도구 이름이 들어 있어도 접속이 아니다
TEXT_TOOLS = {
    "grep", "egrep", "fgrep", "rg", "ag", "ack", "git", "cat", "less", "more", "head", "tail", "find", "fd",
    "ls", "dir", "which", "where", "whereis", "type", "man", "help", "findstr", "select-string", "sls", "wc",
    "sort", "uniq", "diff", "echo", "printf", "get-command", "gcm", "get-help", "file", "stat", "tree",
    "pip", "pip3", "brew", "choco", "winget", "apt", "apt-get", "npm", "pnpm", "yarn", "code", "vim", "vi", "nano",
    "get-childitem", "gci", "test-path", "rm", "del", "remove-item", "mkdir", "touch", "cd", "pushd", "popd",
}
HARNESS = {"vmc-harness"}
_PREFIX = {"sudo", "env", "time", "nohup", "exec", "command", "call", "start", "start-process", "nice", "xargs",
           "timeout", "watch", "&", "builtin", "noglob"}
_SHELL_WRAP = {"cmd": ("/c", "/k"), "powershell": ("-command", "-c", "-encodedcommand"),
               "pwsh": ("-command", "-c"), "bash": ("-c", "-lc"), "sh": ("-c",), "zsh": ("-c", "-lc")}
_RUNNERS = {"uv", "poetry", "pipenv", "pdm", "hatch", "conda", "rye", "pixi"}
_PY = re.compile(r"^(python(\d+(\.\d+)?)?w?|py|pythonw|ipython)$")
_NODE = {"node", "deno", "bun", "tsx", "ts-node"}
# 따옴표 인자를 원격·컨테이너에서 실행하는 명령 — 그 안의 DB 도구는 실행이다(그 밖 명령의 따옴표 인자는 문자열로 본다)
_REMOTE_EXEC = {"ssh", "plink", "docker", "podman", "kubectl", "oc", "wsl", "nerdctl"}
EXEC, TEXT = "exec", "text"
_SCRIPT_EXT = {".py": "py", ".js": "node", ".mjs": "node", ".cjs": "node", ".ts": "node", ".mts": "node", ".cts": "node",
               ".jsx": "node", ".tsx": "node", ".ps1": "ps", ".psm1": "ps", ".sh": "sh", ".bash": "sh", ".zsh": "sh",
               ".bat": "bat", ".cmd": "bat"}

_ODBC_DRIVER = re.compile(r"(?i)\b(driver|dsn)\s*=\s*\{?[^;]*?(sql\s*server|odbc\s+driver|freetds|tds)")
_CONN_STR = re.compile(r"(?i)\b(server|data\s+source|addr|address)\s*=\s*[^;'\"]+;[^'\"]*?\b(database|initial\s+catalog|uid|user\s+id)\s*=")
_URL = re.compile(r"(?i)\b(mssql(\+\w+)?://|sqlserver://|jdbc:sqlserver:)")
_PS_CLIENT = re.compile(r"(?i)\b(invoke-sqlcmd|invoke-dbaquery|system\.data\.sqlclient|microsoft\.data\.sqlclient|sqlconnection\b|sqlcommand\b|system\.data\.odbc)")
_PY_DRIVER = re.compile(r"\b(pyodbc|pymssql|_mssql|pytds|aioodbc|aiomssql)\b|mssql\+")
_PY_CONNECT = re.compile(r"\b(connect|create_engine|create_async_engine)\s*\(")
_PY_APPDB = re.compile(r"(?i)\b(create_engine|create_async_engine|get_engine|get_db|sessionlocal|get_session|get_connection)\s*\("
                       r"|\b\w*(engine|session|conn|connection|cursor)\w*\s*\.\s*(execute|exec_driver_sql|executemany|begin|connect)\s*\(")
_PY_SCRIPT_CONNECT = re.compile(r"(pyodbc|pymssql|_mssql|pytds)\s*\.\s*connect\s*\(|create_engine\s*\(\s*[rfu]?['\"]mssql|mssql\+\w+://")
_NODE_DRIVER = re.compile(r"(require\s*\(\s*['\"](mssql|tedious|msnodesqlv8)['\"]|from\s+['\"](mssql|tedious|msnodesqlv8)['\"])")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def is_direct_db_command(command, cwd=None):
    return direct_db_reason(command, cwd) is not None


def direct_db_reason(command, cwd=None):
    """DB 직접 접속이면 무엇이 걸렸는지 한 줄, 아니면 None."""
    v = direct_db_verdict(command, cwd)
    return v[0] if v else None


def direct_db_verdict(command, cwd=None):
    """(사유, exec|text) 또는 None. 해석 중 예외가 나면 명령 전체를 패턴으로 본다(fail-closed) — 실행 근거가 없으니 text."""
    if not command or not command.strip():
        return None
    try:
        return _analyze(command, cwd, 0)
    except Exception:
        return _text(_raw_patterns(command))


def _exec(reason):
    return (reason, EXEC) if reason else None


def _text(reason):
    return (reason, TEXT) if reason else None


def _unquoted(seg):
    """따옴표 안 문자열을 걷어 낸 segment — 접속 흔적이 따옴표 밖(실행되는 코드)에도 있는지 볼 때."""
    return re.sub(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", " ", seg)


def _raw_patterns(text):
    low = text.lower()
    for tool in sorted(DB_TOOLS):
        if re.search(r"(^|[\s/\\'\"(;&|`])%s(\.exe)?\b" % re.escape(tool), low):
            if tool in ("mssql", "ssms") and not re.search(r"(^|[\s;&|])%s(\s|$)" % tool, low):
                continue
            return "DB 직접 접속 차단됨(%s)" % tool
    if _ODBC_DRIVER.search(text) or _CONN_STR.search(text) or _URL.search(text):
        return "DB 직접 접속 차단됨(연결 문자열)"
    if _PS_CLIENT.search(text):
        return "DB 직접 접속 차단됨(SQL 클라이언트)"
    if _PY_DRIVER.search(text) and _PY_CONNECT.search(text):
        return "DB 직접 접속 차단됨(파이썬 DB 드라이버)"
    if _NODE_DRIVER.search(text):
        return "DB 직접 접속 차단됨(노드 DB 드라이버)"
    return None


def _split_segments(command):
    """따옴표를 지키며 ;, &&, ||, |, &, 줄바꿈으로 나눈다. [(segment, op_before, heredoc_body)]
    줄 안에서 `<<TAG`로 연 heredoc은 다음 줄부터 TAG 줄까지를 그 segment의 본문으로 붙인다."""
    segs, buf, q, i, n = [], [], None, 0, len(command)
    op_before, line_first = None, 0

    def flush(next_op):
        segs.append(["".join(buf), op_before, ""])
        del buf[:]
        return next_op

    while i < n:
        ch = command[i]
        if q:
            buf.append(ch)
            if ch == "\\" and q == '"' and i + 1 < n:
                buf.append(command[i + 1])
                i += 2
                continue
            if ch == q:
                q = None
            i += 1
            continue
        if ch in "'\"":
            q = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            buf.append(ch)
            buf.append(command[i + 1])
            i += 2
            continue
        two = command[i:i + 2]
        if two in ("&&", "||"):
            op_before = flush(two)
            i += 2
            continue
        if ch == "&" and ((i > 0 and command[i - 1] in "<>") or command[i + 1:i + 2] == ">"):
            buf.append(ch)
            i += 1
            continue
        if ch in ";|&":
            op_before = flush(ch)
            i += 1
            continue
        if ch == "\n":
            op_before = flush("\n")
            i += 1
            for k in range(line_first, len(segs)):
                for m in _HEREDOC.finditer(segs[k][0]):
                    if m.start() > 0 and segs[k][0][m.start() - 1] == "<":
                        continue
                    body = []
                    while i < n:
                        j = command.find("\n", i)
                        line = command[i:] if j < 0 else command[i:j]
                        i = n if j < 0 else j + 1
                        if line.strip() == m.group(2):
                            break
                        body.append(line)
                    segs[k][2] += "\n".join(body) + "\n"
            line_first = len(segs)
            continue
        buf.append(ch)
        i += 1
    if q:
        raise ValueError("닫히지 않은 따옴표")
    flush(None)
    return [tuple(s) for s in segs if s[0].strip() or s[2].strip()]


def _words(seg):
    """셸 단어. 백슬래시는 그대로 둔다(Windows 경로) — posix=False 후 바깥 따옴표만 벗긴다."""
    try:
        raw = shlex.split(seg, posix=False)
    except ValueError:
        raw = seg.split()
    out = []
    for w in raw:
        if len(w) >= 2 and w[0] == w[-1] and w[0] in "'\"":
            w = w[1:-1]
        out.append(w)
    return out


def _base(word):
    b = re.split(r"[\\/]", word)[-1].lower()
    for ext in (".exe", ".cmd", ".bat", ".ps1"):
        if b.endswith(ext):
            b = b[: -len(ext)]
    return b


def _analyze(command, cwd, depth):
    if depth > int(config.get("db.command_max_nest", profile="standard")):
        return _text(_raw_patterns(command))
    segs = _split_segments(command)
    for idx, (seg, op, heredoc) in enumerate(segs):
        words = _words(seg)
        # 앞쪽 환경변수 대입·접두 명령 제거
        while words and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]) or _base(words[0]) in _PREFIX):
            words = words[1:]
            while words and words[0].startswith("-") and len(words) > 1:
                words = words[1:]
        if not words:
            continue
        cmd0 = _base(words[0])
        if cmd0 in HARNESS or (_PY.match(cmd0) and len(words) > 2 and words[1] == "-m" and words[2] == "vmc_harness"):
            if "$(" in seg or "`" in seg:
                r = _exec(_raw_patterns(seg))   # 명령 치환은 실행된다
                if r:
                    return r
            continue
        if cmd0 in TEXT_TOOLS:
            continue
        r = _script_file(words[0], cwd, depth)   # ./x.sh · x.bat · & .\x.ps1 — 스크립트 파일을 바로 실행
        if r:
            return r
        if cmd0 in _SHELL_WRAP:
            flags = _SHELL_WRAP[cmd0]
            for k, w in enumerate(words[1:], 1):
                if w.lower() in flags and k + 1 < len(words):
                    r = _analyze(" ".join(words[k + 1:]) if cmd0 in ("cmd", "powershell", "pwsh") else words[k + 1], cwd, depth + 1)
                    if r:
                        return r
                    break
            else:
                script = _wrapped_script(cmd0, words[1:])
                if script:
                    r = _script_file(script, cwd, depth)
                    if r:
                        return r
                elif cmd0 in ("powershell", "pwsh") and len(words) > 1 and not words[1].startswith("-"):
                    r = _analyze(" ".join(words[1:]), cwd, depth + 1)
                    if r:
                        return r
            if heredoc:
                r = _analyze(heredoc, cwd, depth + 1)
                if r:
                    return r
            continue
        if cmd0 in _RUNNERS and "run" in [w.lower() for w in words[1:3]]:
            k = [w.lower() for w in words].index("run") + 1
            while k < len(words) and words[k].startswith("-"):
                k += 1
            words = words[k:]
            if not words:
                continue
            cmd0 = _base(words[0])
        inner = [_base(w) for w in words[1:] if _base(w) in DB_TOOLS and _base(w) not in ("mssql", "ssms")]
        if cmd0 in DB_TOOLS or inner:
            tool = cmd0 if cmd0 in DB_TOOLS else inner[0]
            return _exec("DB 직접 접속 차단됨(%s)" % tool)
        # 접속 흔적이 따옴표 밖(실행되는 코드·인자)에 있으면 실행, 따옴표 안에만 있으면 문자열(JSON 페이로드 등)
        for rx, what in ((_PS_CLIENT, "SQL 클라이언트"), (_ODBC_DRIVER, "연결 문자열"), (_CONN_STR, "연결 문자열"), (_URL, "연결 문자열")):
            if rx.search(seg):
                reason = "DB 직접 접속 차단됨(%s)" % what
                return _exec(reason) if rx.search(_unquoted(seg)) else _text(reason)
        if _PY.match(cmd0):
            r = _python_segment(words, heredoc, segs, idx, op, cwd, depth)
            if r:
                return r
            continue
        if cmd0 in _NODE:
            code = _inline_arg(words, ("-e", "--eval", "-p", "--print")) + "\n" + heredoc + _piped_text(segs, idx, op)
            if _NODE_DRIVER.search(code):
                return _exec("DB 직접 접속 차단됨(노드 DB 드라이버)")
            target = next((w for w in words[1:] if not w.startswith("-") and w.lower() != "run"), None)
            if target:
                r = _script_file(target, cwd, depth)
                if r:
                    return r
            continue
        if heredoc:
            r = _text(_raw_patterns(heredoc))   # cat <<EOF > x.sql 같은 데이터 본문
            if r:
                return r
        # 따옴표로 묶인 인자 안의 셸 명령(예: ssh host "sqlcmd …") — 원격·컨테이너 실행 명령이 아니면 문자열로 본다
        for w in words[1:]:
            if " " in w and len(w) > 3:
                r = _analyze(w, cwd, depth + 1)
                if r:
                    return r if cmd0 in _REMOTE_EXEC else _text(r[0])
    return None


def _wrapped_script(cmd0, args):
    """bash x.sh · powershell -File x.ps1 · cmd /c x.bat(이 경우는 재귀가 처리) — 실행할 스크립트 파일 단어."""
    if cmd0 in ("powershell", "pwsh"):
        for k, a in enumerate(args):
            if a.lower() in ("-file", "-f") and k + 1 < len(args):
                return args[k + 1]
        return None
    if cmd0 in ("bash", "sh", "zsh"):
        k = 0
        while k < len(args) and args[k].startswith("-"):
            k += 1
        return args[k] if k < len(args) else None
    return None


def _script_file(word, cwd, depth, kind=None):
    """스크립트 파일이면 내용을 읽어 DB 직접 접속을 찾는다(실행 근거 — exec). 아니면 None. kind를 주면 확장자 대신 그 언어로."""
    w = (word or "").lstrip("&").strip()
    kind = kind or _SCRIPT_EXT.get(os.path.splitext(w.lower())[1])
    if not kind:
        return None
    f = w.replace("\\", "/")
    base = Path(cwd) if cwd else Path(os.getcwd())
    p = Path(f) if os.path.isabs(f) or re.match(r"^[A-Za-z]:/", f) else base / f
    try:
        if not p.is_file() or p.stat().st_size > config.get("db.script_scan_max_bytes", str(base)):
            return None
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    hit = None
    if kind == "py":
        hit = bool(_PY_SCRIPT_CONNECT.search(text))
    elif kind == "node":
        hit = bool(_NODE_DRIVER.search(text))
    elif kind == "sh":
        try:
            hit = bool(_analyze(text, str(base), depth + 1))
        except Exception:
            hit = bool(_raw_patterns(text))
    else:
        hit = bool(_raw_patterns(text))
    return _exec("DB 직접 접속 차단됨(스크립트의 DB 직접 접속: %s)" % p.name) if hit else None


def _inline_arg(words, flags):
    for k, w in enumerate(words):
        if w in flags and k + 1 < len(words):
            return words[k + 1]
        for f in flags:
            if f.startswith("-") and len(f) == 2 and w.startswith(f) and len(w) > 2 and not w.startswith("--"):
                return w[2:]
    return ""


def _piped_text(segs, idx, op):
    if op != "|" or idx == 0:
        return ""
    prev_seg, _, prev_heredoc = segs[idx - 1]
    return "\n" + prev_seg + "\n" + prev_heredoc


def _python_segment(words, heredoc, segs, idx, op, cwd, depth=0):
    code = _inline_arg(words, ("-c",)) + "\n" + heredoc + _piped_text(segs, idx, op)
    if (_PY_DRIVER.search(code) and _PY_CONNECT.search(code)) or _PY_APPDB.search(code) or _URL.search(code) \
            or _ODBC_DRIVER.search(code) or _CONN_STR.search(code):
        return _exec("DB 직접 접속 차단됨(파이썬 인라인 DB 접속)")
    # python <스크립트.py> / python -m <모듈> — 스크립트 안에 드라이버 직접 접속이 있으면
    target = None
    rest = words[1:]
    k = 0
    while k < len(rest):
        w = rest[k]
        if w == "-m" and k + 1 < len(rest):
            mod = rest[k + 1]
            if mod.split(".")[0] in ("vmc_harness", "pytest", "pip", "unittest", "venv", "http"):
                return None
            target = ("module", mod)
            break
        if w == "-c":
            return None
        if w.startswith("-"):
            k += 1
            continue
        target = ("file", w)
        break
    if not target:
        return None
    if target[0] == "file":
        return _script_file(target[1], cwd, depth, kind="py")
    base = Path(cwd) if cwd else Path(os.getcwd())
    rel = Path(*target[1].split("."))
    cands = [base / (str(rel) + ".py"), base / rel / "__main__.py"]
    from vmc_harness.core import config
    limit = config.get("db.script_scan_max_bytes", str(base))  # cwd를 넘겨 getcwd에 기대지 않는다
    for p in cands:
        try:
            if p.is_file() and p.stat().st_size <= limit:
                text = p.read_text(encoding="utf-8", errors="replace")
                if _PY_SCRIPT_CONNECT.search(text):
                    return _exec("DB 직접 접속 차단됨(스크립트의 DB 드라이버 직접 접속: %s)" % p.name)
        except OSError:
            continue
    return None
