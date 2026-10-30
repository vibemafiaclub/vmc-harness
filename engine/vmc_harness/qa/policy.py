"""테스트 데이터 범위 `.harness/qa/data-policy.json` (spec 05).

개발 DB에서 QA가 바꿔도 되는 범위·금지 범위·DB 직접 접근 금지(화면으로만)를 적는다.
QA 실행기(runner)가 시나리오의 선언 쓰기를 이 범위로 검사하고, QA 진행 중 훅(qa.hooks)이 범위 밖 쓰기를 막는다.

형식(기본값):
{
  "db_access": "browser_only",                     # DB는 화면(브라우저)으로만. 직접 접속 명령은 막는다
  "allowed": [{"entity": "work_requests", "ops": ["create","update"], "match": "QA_*", "note": "QA_ 접두 데이터만"}],
  "forbidden": [{"entity": "audit_*", "ops": ["create","update","delete"], "reason": "감사 기록"}],
  "forbidden_urls": ["*/admin/users/*/delete*"],  # 브라우저·HTTP로 열면 안 되는 주소 glob
  "write_methods": ["POST","PUT","PATCH","DELETE"],
  "allowed_hosts": ["localhost", "127.0.0.1", "dev.example.local"]   # HTTP 쓰기가 허용되는 호스트(개발 서버)
}
- 금지가 허용보다 우선. 허용 목록에 없는 쓰기는 범위 밖이다(빈 허용 목록 = 쓰기 없는 시나리오만).
- 범위를 넓히는 변경(허용 늘리기·금지 줄이기·db_access 바꾸기)은 안전 경계를 넓히는 일(사람 개입 기준 2) —
  에이전트 편집은 qa.hooks가 막고, 사람이 직접 고친다.
"""

import fnmatch
import re

from vmc_harness.core import paths
from vmc_harness.core.jsonio import read_json, write_json

DEFAULT_POLICY = {
    "db_access": "browser_only",
    "allowed": [],
    "forbidden": [],
    "forbidden_urls": [],
    "write_methods": ["POST", "PUT", "PATCH", "DELETE"],
    "allowed_hosts": ["localhost", "127.0.0.1"],
}

# db 영역 sqlguard가 없을 때의 대체 판정(안전 쪽 — 넓게 막는다)
_DIRECT_DB = re.compile(
    r"(?i)(\bsqlcmd\b|\bosql\b|\bbcp\b|invoke-sqlcmd|\bpyodbc\b|\bpymssql\b|\bpsql\b|\bmysql\b|\bsqlite3\b|"
    r"\bmongo(sh)?\b|\bredis-cli\b|Server=[^;]+;|Data Source=|Initial Catalog=|DRIVER=\{|"
    r"\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|TRUNCATE\s+TABLE|DROP\s+TABLE|ALTER\s+TABLE|EXEC(UTE)?\s+\w+))"
)
_URL = re.compile(r"https?://[^\s'\"<>]+")
_METHOD = re.compile(r"(?:-X|--request)\s*['\"]?([A-Za-z]+)")


def policy_path(root):
    return paths.harness_dir(root) / "qa" / "data-policy.json"


def load(root):
    data = read_json(policy_path(root), None)
    if not isinstance(data, dict):
        return None
    out = dict(DEFAULT_POLICY)
    out.update(data)
    return out


def ensure(root):
    """없으면 기본값(쓰기 허용 없음·DB 직접 접근 금지)으로 만든다."""
    p = policy_path(root)
    if not p.exists():
        write_json(p, DEFAULT_POLICY)
    return load(root)


def _match(pattern, value):
    return fnmatch.fnmatchcase(str(value or "").lower(), str(pattern or "").lower())


def check_data_write(policy, entity, op, record=None):
    """(ok, reason). 선언된 데이터 쓰기 1건 검사."""
    if policy is None:
        return False, "데이터 범위 설정 없음"
    for f in policy.get("forbidden") or []:
        if _match(f.get("entity"), entity) and op in (f.get("ops") or ["create", "update", "delete"]):
            return False, "금지 범위 쓰기: %s %s" % (op, entity)
    for a in policy.get("allowed") or []:
        if _match(a.get("entity"), entity) and op in (a.get("ops") or []):
            m = a.get("match")
            if m and record is not None and not _match(m, record):
                continue
            return True, None
    return False, "허용 범위 밖 쓰기: %s %s" % (op, entity)


def is_direct_db_command(command):
    try:
        from vmc_harness.db.sqlguard import is_direct_db_command as f
        if f(command):
            return True
    except (ImportError, AttributeError):
        pass
    return bool(_DIRECT_DB.search(command or ""))


def _host(url):
    m = re.match(r"https?://([^/:?#]+)", url or "")
    return (m.group(1) if m else "").lower()


def check_write(root, target, policy=None):
    """범위 밖 쓰기 판정 (ok, reason). gates·qa 훅이 부른다.

    target 예:
      {"kind": "data", "entity": "work_requests", "op": "create", "record": "QA_001"}
      {"kind": "command", "command": "sqlcmd -S ..."}
      {"kind": "http", "url": "http://localhost:3000/api/x", "method": "DELETE"}
      {"kind": "url", "url": "http://localhost:3000/admin/users/3/delete"}   # 브라우저 이동
    정책 파일이 없으면 기본값(쓰기 허용 없음·DB 직접 접근 금지)으로 판정한다.
    """
    policy = policy or load(root) or dict(DEFAULT_POLICY)
    kind = (target or {}).get("kind")
    if kind == "data":
        return check_data_write(policy, target.get("entity"), target.get("op"), target.get("record"))
    if kind == "command":
        cmd = target.get("command") or ""
        if policy.get("db_access", "browser_only") == "browser_only" and is_direct_db_command(cmd):
            return False, "QA 중 DB 직접 접근(화면으로만)"
        for url in _URL.findall(cmd):
            m = _METHOD.search(cmd)
            method = (m.group(1).upper() if m else ("POST" if re.search(r"\s(-d|--data\S*|-F|--form)\s", cmd) else "GET"))
            ok, reason = check_write(root, {"kind": "http", "url": url, "method": method}, policy)
            if not ok:
                return ok, reason
        return True, None
    if kind in ("http", "url"):
        url = target.get("url") or ""
        for g in policy.get("forbidden_urls") or []:
            if _match(g, url):
                return False, "금지 주소: %s" % url
        method = (target.get("method") or "GET").upper()
        if kind == "http" and method in [m.upper() for m in policy.get("write_methods") or []]:
            hosts = [h.lower() for h in policy.get("allowed_hosts") or []]
            if _host(url) not in hosts:
                return False, "개발 서버 밖 쓰기 요청: %s %s" % (method, _host(url))
        return True, None
    return True, None


def is_widening(old, new):
    """사람만 할 수 있는 범위 넓히기인가 (bool, 이유)."""
    old = old or dict(DEFAULT_POLICY)
    new = new or {}
    if (new.get("db_access") or "browser_only") != (old.get("db_access") or "browser_only"):
        return True, "db_access 변경"

    def keyset(items, field):
        out = set()
        for it in items or []:
            for op in it.get(field) or []:
                out.add((str(it.get("entity")), op, str(it.get("match") or "")))
        return out

    if keyset(new.get("allowed"), "ops") - keyset(old.get("allowed"), "ops"):
        return True, "허용 범위 늘림"
    fo = {(str(f.get("entity")), op) for f in old.get("forbidden") or [] for op in (f.get("ops") or ["create", "update", "delete"])}
    fn = {(str(f.get("entity")), op) for f in new.get("forbidden") or [] for op in (f.get("ops") or ["create", "update", "delete"])}
    if fo - fn:
        return True, "금지 범위 줄임"
    if set(old.get("forbidden_urls") or []) - set(new.get("forbidden_urls") or []):
        return True, "금지 주소 줄임"
    if set(h.lower() for h in new.get("allowed_hosts") or []) - set(h.lower() for h in old.get("allowed_hosts") or []):
        return True, "쓰기 허용 호스트 늘림"
    return False, None
