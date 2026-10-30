"""`vmc-harness db-credential` 본체 (spec 08·10). 사람이 터미널에서 직접 입력한다 — 대화로 받지 않는다(P9).

순서: 입력(서버·포트·DB·계정, 비밀번호는 getpass) → 운영 이름 거부 → "개발/복제 DB인가요" 확인 →
드라이버 확인(없으면 설치 시도) → 연결 → 운영 이름 재확인(@@SERVERNAME·DB_NAME()) →
query 프로파일이면 쓰기 권한 없는지 확인 → 키체인 저장 + db.json → 기계 사실(호환성 수준) 저장.
"""

import sys

from vmc_harness.core import config, secrets
from vmc_harness.core.clock import now_iso
from vmc_harness.db import client, drivers, facts

# 읽기 전용 확인 — 하나라도 1이면 쓰기 가능 계정
WRITE_CHECKS = [
    ("sysadmin", "SELECT IS_SRVROLEMEMBER('sysadmin')"),
    ("db_owner", "SELECT IS_ROLEMEMBER('db_owner')"),
    ("db_datawriter", "SELECT IS_ROLEMEMBER('db_datawriter')"),
    ("db_ddladmin", "SELECT IS_ROLEMEMBER('db_ddladmin')"),
    ("INSERT", "SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'INSERT')"),
    ("UPDATE", "SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'UPDATE')"),
    ("DELETE", "SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'DELETE')"),
    ("ALTER", "SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'ALTER')"),
    ("CREATE TABLE", "SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', 'CREATE TABLE')"),
]


class CredentialRefused(RuntimeError):
    pass


def write_permissions(conn):
    """쓰기 가능 근거 목록(빈 목록이면 읽기 전용)."""
    out = []
    for label, sql in WRITE_CHECKS:
        try:
            if conn.scalar(sql) == 1:
                out.append(label)
        except Exception:
            continue
    return out


def register(root, profile, fields, password, confirmed_dev, conn_factory=None, log=print):
    """입력값으로 등록. fields: server, port, database, user, (driver, trust_server_certificate).
    거부면 CredentialRefused(무엇을 어겼는지). 성공하면 저장한 info."""
    if profile not in client.PROFILES:
        raise CredentialRefused("프로파일은 query 또는 dryrun")
    for k in ("server", "database", "user"):
        if not (fields.get(k) or "").strip():
            raise CredentialRefused("%s 입력 없음" % k)
    if not password:
        raise CredentialRefused("비밀번호 입력 없음")
    hit = client.looks_production(root, fields["server"], fields["database"])
    if hit:
        raise CredentialRefused("운영 DB로 보이는 이름(%s) — 운영 DB 접속 정보는 등록하지 않음" % hit)
    if not confirmed_dev:
        raise CredentialRefused("개발/복제 DB 확인 없음 — 운영 DB 접속 정보는 등록하지 않음")
    info = {"server": fields["server"].strip(), "port": (str(fields.get("port") or "").strip() or None),
            "database": fields["database"].strip(), "user": fields["user"].strip(),
            "trust_server_certificate": bool(fields.get("trust_server_certificate", True)),
            "secret_ref": client.secret_name(root, profile), "registered_at": now_iso()}
    if conn_factory is None:
        if not drivers.available():
            ok, last = drivers.try_install(log=log, root=root)
            if not ok:
                raise CredentialRefused("MSSQL 드라이버 설치 실패(pymssql): %s" % last)
        info["driver"] = fields.get("driver") or drivers.pick()
        conn = drivers.connect(info, password, config.get("db.query_timeout_s", root), root=root)
    else:
        info["driver"] = fields.get("driver") or "fake"
        conn = conn_factory(info, password, config.get("db.query_timeout_s", root))
    try:
        server_name = conn.scalar("SELECT @@SERVERNAME")
        db_name = conn.scalar("SELECT DB_NAME()")
        hit = client.looks_production(root, server_name, db_name)
        if hit:
            raise CredentialRefused("접속한 서버·DB 이름(%s)이 운영으로 보임 — 등록하지 않음" % hit)
        if profile == "query":
            perms = write_permissions(conn)
            if perms:
                raise CredentialRefused("읽기 전용 계정 아님(%s) — 조회용은 읽기 전용 계정만" % ", ".join(perms))
        info["server_name"] = server_name
        stored_in = secrets.set_secret(info["secret_ref"], password)
        info["secret_store"] = stored_in
        client.save_profile(root, profile, info)
        if profile == "query":  # 기계 사실은 개발 DB(조회 대상) 기준 — 드라이런 DB 값은 드라이런 결과에 따로 남는다
            facts.collect(root, conn, profile)
    finally:
        conn.rollback()
        conn.close()
    lvl = facts.value(root, "compatibility_level")
    log("등록됨(%s): %s / %s — 비밀번호 저장소: %s" % (profile, info["server"], info["database"], stored_in))
    if lvl is not None and profile == "query":
        log("기계 사실: 호환성 수준 %s (읽은 명령: %s)" % (lvl, facts.QUERIES[0][1]))
    return info


def interactive(root, profile, prefill=None, out=print):
    """터미널 입력. 표준 입력이 터미널이 아니면 거부(에이전트가 파이프로 넣지 못하게)."""
    if not sys.stdin or not sys.stdin.isatty():
        raise CredentialRefused("터미널 직접 입력 아님 — DB 접속 정보는 사람이 터미널에서 입력")
    prefill = prefill or {}
    label = "개발 DB 읽기 전용 계정" if profile == "query" else "드라이런용 개발/복제 DB 계정(항상 롤백)"
    out("[%s] 등록 — 운영 DB 정보는 넣지 마세요." % label)

    def ask(key, prompt, default=None):
        if prefill.get(key):
            return prefill[key]
        v = input("%s%s: " % (prompt, " [%s]" % default if default else "")).strip()
        return v or (default or "")

    fields = {
        "server": ask("server", "서버(호스트 또는 호스트\\인스턴스)"),
        "port": ask("port", "포트(비우면 기본)", ""),
        "database": ask("database", "DB 이름"),
        "user": ask("user", "계정"),
    }
    trust = ask("trust", "서버 인증서를 검증 없이 신뢰(개발 서버 자체 서명)? y/n", "y")
    fields["trust_server_certificate"] = trust.lower().startswith("y")
    hit = client.looks_production(root, fields["server"], fields["database"])
    if hit:
        raise CredentialRefused("운영 DB로 보이는 이름(%s) — 운영 DB 접속 정보는 등록하지 않음" % hit)
    password = secrets.read_secret_from_terminal("비밀번호(화면에 안 보임): ")
    ans = input("이 DB는 개발 또는 복제 DB입니까? 운영 DB면 등록하지 않습니다. (yes 입력): ").strip().lower()
    return register(root, profile, fields, password, ans in ("yes", "y", "예", "네"), log=out)
