"""DB 기계 사실 (spec 01 P3·07 부트스트랩). 읽은 명령과 함께 저장하고, 가끔 다시 읽어 바뀌면 갱신한다.

저장: `~/.vmc-harness/projects/<project_id>/db-facts.json` (PC 로컬 — 서버·DB 이름이 들어 있다)
    {"facts": [{"key", "value", "command", "read_at", "profile", "changed_from"?}], "db_kind": "mssql", "read_at"}
부트스트랩은 `load(root)`로 받아 rules.json(기계 사실 규칙)·facts에 넣는다. 팀 장부로 갈 때는 key·value·command만.
"""

import datetime as _dt

from vmc_harness.core import config
from vmc_harness.core.clock import now, now_iso, parse_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.project import local_dir

# (key, 명령) — 결정적으로 읽는 값만
QUERIES = [
    ("compatibility_level", "SELECT compatibility_level FROM sys.databases WHERE name = DB_NAME()"),
    ("product_version", "SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128))"),
    ("collation", "SELECT CAST(DATABASEPROPERTYEX(DB_NAME(), 'Collation') AS nvarchar(128))"),
]
# 호환성 수준별로 쓸 수 없는 내장 기능(공식 문서 기준) — 규칙 블록 환경 사실용
COMPAT_MIN = [("OPENJSON", 130), ("STRING_SPLIT", 130), ("STRING_AGG", 140), ("TRIM", 140),
              ("TRANSLATE", 140), ("CONCAT_WS", 140), ("GREATEST", 160), ("LEAST", 160)]


def _path(root):
    return local_dir(root) / "db-facts.json"


def load(root):
    return read_json(_path(root), {}) or {}


def value(root, key):
    for f in load(root).get("facts", []):
        if f.get("key") == key:
            return f.get("value")
    return None


def unavailable_features(level):
    """호환성 수준에서 못 쓰는 내장 함수 이름 목록."""
    if level is None:
        return []
    return [name for name, need in COMPAT_MIN if int(level) < need]


def collect(root, conn, profile="query"):
    """연결된 conn으로 사실을 읽어 저장. 이전 값과 다르면 changed_from을 남긴다. 저장한 dict를 돌려준다."""
    old = {f["key"]: f for f in load(root).get("facts", [])}
    facts = []
    for key, sql in QUERIES:
        try:
            v = conn.scalar(sql)
        except Exception:
            continue
        rec = {"key": key, "value": v, "command": sql, "read_at": now_iso(), "profile": profile}
        if key in old and old[key].get("value") != v:
            rec["changed_from"] = old[key].get("value")
        facts.append(rec)
    lvl = next((f["value"] for f in facts if f["key"] == "compatibility_level"), None)
    if lvl is not None:
        facts.append({"key": "unavailable_features", "value": unavailable_features(lvl),
                      "command": QUERIES[0][1], "read_at": now_iso(), "profile": profile,
                      "derived": "호환성 수준 → 내장 함수 최소 수준 표"})
    data = {"db_kind": "mssql", "read_at": now_iso(), "facts": facts}
    write_json(_path(root), data)
    return data


def stale(root):
    d = load(root)
    at = parse_iso(d.get("read_at"))
    if not at:
        return True
    return now() - at > _dt.timedelta(days=config.get("db.facts_recheck_days", root))


def refresh(root, conn_factory=None):
    """등록된 query 프로파일로 다시 읽는다. 실패하면 None(편의 경로 — 기존 값 유지)."""
    from vmc_harness.db import client
    try:
        used, info, conn = client._connect(root, "query", config.get("db.query_timeout_s", root), conn_factory)
    except Exception:
        return None
    try:
        return collect(root, conn, used)
    finally:
        conn.rollback()
        conn.close()
