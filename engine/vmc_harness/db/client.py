"""DB 조회·드라이런 (spec 02 §DB 안전장치).

- 접속 정보: `~/.vmc-harness/projects/<project_id>/db.json` — 프로파일별 서버·DB·계정·드라이버·키체인 참조만.
  비밀번호는 core.secrets(키체인 이름 `db:<project_id>:<profile>`)
  - `query`  : 개발 DB **읽기 전용** 계정 — `db query`가 쓴다
  - `dryrun` : 개발/복제 DB 계정(쓰기 권한 가능 — 항상 ROLLBACK) — `db dryrun`이 쓴다. 없으면 `query`로 시도
- `query(root, sql)` → {"columns","rows","truncated"} (+ "auto_top")
- `dryrun(root, sql_path, task_id=None)` → {"affected_rows","statements", …} — 트랜잭션 안 실행 → 항상 ROLLBACK
- `prepare_apply_approval(root, task_id, sql_path)` — 운영 적용 승인 기록(롤백 SQL·드라이런 결과 필수)
- `patch_dir(root)` — SQL 패치 폴더(`.harness/config.json`의 db.patch_dir가 우선)
"""

import hashlib
import re
import threading
import time
from pathlib import Path

from vmc_harness.core import config, events, paths
from vmc_harness.core.clock import now_iso
from vmc_harness.core.jsonio import read_json, write_json
from vmc_harness.core.project import load_config, local_dir, project_id
from vmc_harness.db import drivers, sqlguard

PROFILES = ("query", "dryrun")
COMPAT_SQL = "SELECT compatibility_level FROM sys.databases WHERE name = DB_NAME()"


class DbRefused(ValueError):
    """하네스가 실행 전에 거부(메시지 = 무엇을 어겼는지)."""


class NotRegistered(RuntimeError):
    pass


# ---------------------------------------------------------------- 설정·접속 정보

def db_json_path(root):
    return local_dir(root) / "db.json"


def load_db_json(root):
    return read_json(db_json_path(root), {}) or {}


def save_profile(root, profile, info):
    if profile not in PROFILES:
        raise ValueError(profile)
    data = load_db_json(root)
    data[profile] = info
    write_json(db_json_path(root), data)


def remove_profile(root, profile):
    from vmc_harness.core import secrets
    data = load_db_json(root)
    info = data.pop(profile, None)
    if info and info.get("secret_ref"):
        secrets.delete_secret(info["secret_ref"])
    write_json(db_json_path(root), data)
    return info


def secret_name(root, profile):
    return "db:%s:%s" % (project_id(root), profile)


def get_profile(root, profile):
    """(profile 이름, info). dryrun이 없으면 query로 대체."""
    data = load_db_json(root)
    if profile in data:
        return profile, data[profile]
    if profile == "dryrun" and "query" in data:
        return "query", data["query"]
    raise NotRegistered("DB 접속 정보 미등록(%s)" % profile)


def _harness_db_cfg(root):
    cfg = load_config(root)
    db = cfg.get("db") if isinstance(cfg.get("db"), dict) else {}
    out = dict(db)
    for k, v in cfg.items():
        if k.startswith("db."):
            out[k[3:]] = v
    return out


def patch_dir(root):
    """SQL 패치 폴더(프로젝트 루트 상대, `/` 구분). DB를 쓰는 프로젝트가 아니면 None(게이트 비활성).

    DB 프로젝트 판정: `.harness/config.json`에 db.patch_dir가 있다(부트스트랩이 기존 관례에서 찾아 쓴다)
    / db.json이 등록돼 있다 / 부트스트랩 기계 사실에 DB 종류가 있다."""
    own = _harness_db_cfg(root).get("patch_dir")
    if own:
        return str(own).replace("\\", "/").strip("/")
    active = False
    try:
        active = bool(load_db_json(root))
    except Exception:
        active = False
    if not active:
        try:
            from vmc_harness.bootstrap import facts as bfacts
            f = bfacts.load(root) or {}
            active = bool(f.get("db_kind") or f.get("db") or f.get("database"))
        except Exception:
            pass
    if not active:
        return None
    return str(config.get("db.patch_dir", root)).replace("\\", "/").strip("/")


def prod_names(root):
    """`.harness/config.json` db.prod_names — 사람이 운영이라고 적은 서버·DB 이름."""
    v = _harness_db_cfg(root).get("prod_names") or []
    return [str(x) for x in v] if isinstance(v, list) else [str(v)]


def prod_allow(root):
    """`.harness/config.json` prod_allow — 사람이 "운영 아님"으로 푼 칸(12 운영 이름 오탐).
    {"names": [서버·DB 이름(대소문자 무시, 통째로 같을 때만)], "files": [운영 설정 파일로 보지 않을 경로 glob]}.
    칸을 늘리는 L1 변경은 사람 승인(메타 게이트 prod_allow) — 에이전트가 스스로 풀 수 없다."""
    v = load_config(root).get("prod_allow") if root else None
    v = v if isinstance(v, dict) else {}
    out = {}
    for k in ("names", "files"):
        x = v.get(k) or []
        out[k] = [str(i) for i in x] if isinstance(x, list) else [str(x)]
    return out


def _name_tokens(name):
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", name or "")
    s = re.sub(r"([A-Za-z])(\d)|(\d)([A-Za-z])", lambda m: " ".join(x for x in m.groups() if x), s)
    return [t for t in re.split(r"[^0-9A-Za-z가-힣]+", s.lower()) if t]


def _token_hit(token, pat):
    """이름 조각이 패턴과 같거나 패턴으로 시작·끝난다(SQLPROD·prd01·DIPortalProd). product·delivery 같은 일반 단어는 제외."""
    if token == pat:
        return True
    if token.startswith("product") and pat in ("prod", "prd"):
        return False
    return token.startswith(pat) or token.endswith(pat)


def _token_exact(token, pat):
    """엄격 판정 — 조각이 패턴 그 자체이거나 숫자만 붙었다(prod·prd01·2live). realtime·liveness 같은 단어는 아니다."""
    return bool(re.match(r"^\d*%s\d*$" % re.escape(pat), token))


def looks_production(root, *names, strict=False):
    """운영으로 보이면 걸린 이름, 아니면 None. 영문 패턴은 이름 조각 단위, 한글 패턴은 포함 여부.
    strict=True(파일 이름·환경 값 — 막는 근거가 이름뿐일 때)면 조각이 패턴과 같을 때만(숫자 붙임 허용)."""
    hit = _token_exact if strict else _token_hit
    pats = [p.lower() for p in config.get("db.prod_name_patterns", root)]
    explicit = [p.lower() for p in prod_names(root)]
    allowed = {p.lower() for p in prod_allow(root)["names"]}
    for name in names:
        if not name:
            continue
        low = str(name).lower()
        if low in allowed and low not in explicit:
            continue
        if low in explicit or any(low.split("\\")[0] == e or low.split(",")[0] == e for e in explicit):
            return name
        toks = _name_tokens(str(name))
        for p in pats:
            if re.search(r"[가-힣]", p):
                if p in low:
                    return name
            elif any(hit(t, p) for t in toks):
                return name
    return None


# ---------------------------------------------------------------- 접속·실행 공통

def _connect(root, profile, timeout_s, conn_factory=None):
    from vmc_harness.core import secrets
    used, info = get_profile(root, profile)
    pw = secrets.get_secret(info.get("secret_ref") or secret_name(root, used))
    if pw is None:
        raise NotRegistered("DB 비밀번호가 키체인에 없음(%s)" % used)
    if conn_factory:
        return used, info, conn_factory(info, pw, timeout_s)
    if not drivers.available():
        ok, last = drivers.try_install(root=root)
        if not ok:
            raise drivers.DriverMissing("MSSQL 드라이버 없음 — pymssql 설치 시도 실패: %s" % last)
    return used, info, drivers.connect(info, pw, timeout_s, root=root)


def _with_deadline(conn, seconds, fn, root=None):
    """fn()을 별도 스레드에서. seconds를 넘기면 conn.cancel() → 끝내 안 돌아오면 연결을 버린다(클라이언트측 중단)."""
    box = {}

    def work():
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: B902
            box["error"] = e

    t = threading.Thread(target=work, name="vmh-db", daemon=True)
    started = time.time()
    t.start()
    t.join(seconds)
    if t.is_alive():
        conn.cancel()
        t.join(config.get("db.cancel_grace_s", root))
        if t.is_alive():
            conn.close()
        raise drivers.DbTimeout("쿼리 시간 초과(%ss) — 중단함" % seconds)
    if "error" in box:
        err = box["error"]
        # 드라이버 쪽 타임아웃이 먼저 터지면 오류 코드만 온다(pymssql 20003·20047 — 실측) — 걸린 시간으로 가른다
        near = time.time() - started >= seconds - config.get("db.cancel_grace_s", root)
        if isinstance(err, drivers.DbError) and (near or _is_timeout_text(str(err))):
            raise drivers.DbTimeout("쿼리 시간 초과(%ss) — 중단함" % seconds)
        raise err
    return box.get("value")


def _is_timeout_text(text):
    low = text.lower()
    return "timeout" in low or "timed out" in low or "hyt00" in low or "query cancel" in low


def _session_prep(root, timeout_s, rowcount=None):
    lock_ms = int(timeout_s * 1000)
    sql = ("SET NOCOUNT ON; SET LOCK_TIMEOUT %d; SET DEADLOCK_PRIORITY LOW; SET IMPLICIT_TRANSACTIONS OFF;"
           % lock_ms)
    if rowcount:
        sql += " SET ROWCOUNT %d;" % int(rowcount)
    return sql


# ---------------------------------------------------------------- 조회

def query(root, sql, conn_factory=None):
    """SELECT 하나만. 읽기 전용 계정 + 트랜잭션 안 실행 후 ROLLBACK(이중 안전) + 타임아웃 + 행 상한."""
    ok, reason = sqlguard.is_select_only(sql)
    t0 = time.time()
    if not ok:
        events.log(root, "db_query", outcome="refused", reason=reason, sql=sqlguard.masked_sql(sql))
        raise DbRefused(reason)
    top_n = config.get("db.auto_top_rows", root)
    max_rows = config.get("db.result_max_rows", root)
    timeout_s = config.get("db.query_timeout_s", root)
    run_sql, how = sqlguard.apply_auto_top(sql, top_n)
    used, info, conn = _connect(root, "query", timeout_s, conn_factory)
    try:
        conn.run(_session_prep(root, timeout_s, top_n if how == "rowcount" else None))
        conn.run("BEGIN TRANSACTION")
        res = _with_deadline(conn, timeout_s, lambda: conn.run(run_sql, max_rows=max_rows), root)
    except drivers.DbTimeout as e:
        events.log(root, "db_query", outcome="timeout", sql=sqlguard.masked_sql(sql), sec=round(time.time() - t0, 3))
        raise e
    except drivers.DbError as e:
        events.log(root, "db_query", outcome="error", sql=sqlguard.masked_sql(sql), error=str(e),
                   sec=round(time.time() - t0, 3))
        raise
    finally:
        conn.rollback()
        conn.close()
    out = {"columns": res["columns"], "rows": res["rows"], "truncated": bool(res["truncated"]), "auto_top": how,
           "max_rows": max_rows}
    events.log(root, "db_query", outcome="ok", sql=sqlguard.masked_sql(sql), rows=len(out["rows"]),
               truncated=out["truncated"], auto_top=how, sec=round(time.time() - t0, 3), driver=conn.driver)
    return out


# ---------------------------------------------------------------- 드라이런

def read_sql_file(path):
    data = Path(path).read_bytes()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")
    for enc in ("utf-8-sig", "cp949"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def _rel(root, p):
    try:
        return Path(p).resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return Path(p).resolve().as_posix()


def find_rollback_sql(sql_path):
    """같은 폴더 관례: <이름>_rollback.sql · <이름>.rollback.sql · <이름>_rb.sql · rollback/<이름>.sql
    · Rollback/<이름>.sql · 이름의 PATCH→ROLLBACK. 없으면 None."""
    p = Path(sql_path)
    stem, d = p.stem, p.parent
    names = [stem + "_rollback.sql", stem + ".rollback.sql", stem + "_rb.sql", stem + "-rollback.sql",
             stem + "_ROLLBACK.sql"]
    if re.search(r"(?i)patch", stem):
        names.append(re.sub(r"(?i)patch", "ROLLBACK", stem) + p.suffix)
    cands = [d / n for n in names]
    for sub in ("rollback", "Rollback", "ROLLBACK", "rollbacks"):
        cands.append(d / sub / p.name)
        cands.append(d.parent / sub / p.name)
    for c in cands:
        if c.is_file() and c.resolve() != p.resolve():
            return c
    # 대소문자 무시 비교(Windows 외 파일시스템)
    lower = {n.lower() for n in names}
    for c in d.glob("*.sql"):
        if c.name.lower() in lower and c.resolve() != p.resolve():
            return c
    return None


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dryrun_store(root):
    return paths.cache_dir(root) / "db" / "dryrun"


def dryrun(root, sql_path, task_id=None, conn_factory=None):
    """패치 SQL을 등록된 개발/복제 DB에서 BEGIN TRAN 안에서 실행 → 영향 행 수 집계 → 항상 ROLLBACK."""
    p = Path(sql_path)
    if not p.is_absolute():
        p = Path(root) / p
    if not p.is_file():
        raise DbRefused("SQL 파일 없음: %s" % sql_path)
    text = read_sql_file(p)
    rel = _rel(root, p)
    ignored_use, prepared = [], []
    for body, line, rep in sqlguard.split_batches(text):
        body2, removed = sqlguard.strip_use_lines(body)  # `USE [db]` 줄은 빼고 등록된 DB에서만 돈다
        ignored_use.extend(removed)
        if body2.strip():
            prepared.append((body2, line, rep))
    blockers = sqlguard.dryrun_blockers("\n".join(b for b, _, _ in prepared))
    rb = find_rollback_sql(p)
    result = {
        "file": rel, "sha256": _sha(text), "at": now_iso(), "affected_rows": 0, "statements": 0,
        "batches": len(prepared), "per_batch": [], "error": None, "rolled_back": False,
        "rollback_sql": _rel(root, rb) if rb else None,
        "rollback_note": None if rb else "롤백 SQL 없음",
        "ignored_use": ignored_use, "profile": None, "server": None, "database": None,
        "compatibility_level": None, "warnings": [], "count_method": None,
    }
    if blockers:
        result["error"] = "드라이런 불가 문장: %s" % ", ".join(blockers)
        _finish_dryrun(root, result, task_id, refused=True)
        raise DbRefused(result["error"])
    timeout_s = config.get("db.dryrun_timeout_s", root)
    used, info, conn = _connect(root, "dryrun", timeout_s, conn_factory)
    result["profile"] = used
    t0 = time.time()
    try:
        server_name = conn.scalar("SELECT @@SERVERNAME")
        db_name = conn.scalar("SELECT DB_NAME()")
        result["server"], result["database"] = info.get("server"), db_name
        hit = looks_production(root, info.get("server"), info.get("database"), server_name, db_name)
        if hit:
            raise DbRefused("운영으로 보이는 DB(%s)에는 드라이런하지 않음" % hit)
        try:
            result["compatibility_level"] = conn.scalar(COMPAT_SQL)
        except drivers.DbError:
            pass
        conn.run("SET NOCOUNT OFF; SET LOCK_TIMEOUT %d;" % int(timeout_s * 1000))
        try:
            conn.run("SET STATISTICS PROFILE ON")  # 문장별 실제 행 수(드라이버와 무관) — SHOWPLAN 권한 필요
            result["count_method"] = "statistics_profile"
        except drivers.DbError:
            result["count_method"] = "rowcount"
        conn.run("BEGIN TRANSACTION")
        deadline = t0 + timeout_s
        for idx, (body, line, rep) in enumerate(prepared):
            for _ in range(max(1, rep)):
                left = deadline - time.time()
                if left <= 0:
                    raise drivers.DbTimeout("드라이런 시간 초과(%ss) — 중단함" % timeout_s)
                try:
                    res = _with_deadline(conn, left, lambda b=body: conn.run(b), root)
                except drivers.DbError as e:
                    result["error"] = "배치 %d(%d행~) 오류: %s" % (idx + 1, line, e)
                    raise
                if result["count_method"] == "statistics_profile":
                    dml = [r for stmt, r in res.get("profiles") or [] if is_write_statement(stmt)]
                else:
                    dml = res["rowcounts"]
                n = int(sum(dml))
                result["per_batch"].append({"batch": idx + 1, "line": line, "affected_rows": n,
                                            "statements": len(dml)})
                result["affected_rows"] += n
                result["statements"] += 1
                tc = conn.scalar("SELECT @@TRANCOUNT")
                if not tc:
                    result["error"] = "배치 %d(%d행~)가 드라이런 트랜잭션을 끝냄(패치 안 COMMIT/ROLLBACK) — 이후 실행 중단" % (idx + 1, line)
                    raise drivers.DbError(result["error"])
    except DbRefused as e:
        result["error"] = str(e)
        conn.rollback()
        conn.close()
        result["rolled_back"] = True
        _finish_dryrun(root, result, task_id, refused=True)
        raise
    except drivers.DbTimeout as e:
        result["error"] = str(e)
    except drivers.DbError as e:
        result["error"] = result["error"] or str(e)
    finally:
        if not result.get("rolled_back"):
            try:
                conn.run("IF @@TRANCOUNT > 0 ROLLBACK TRANSACTION")
            except Exception:
                pass
            conn.rollback()
            result["rolled_back"] = True
            conn.close()
    result["sec"] = round(time.time() - t0, 3)
    _compat_warning(root, result)
    _finish_dryrun(root, result, task_id)
    return result


def is_write_statement(stmt):
    """STATISTICS PROFILE 문장이 데이터 변경(INSERT·UPDATE·DELETE·MERGE·SELECT INTO)인지."""
    try:
        toks = [t.up for t in sqlguard.tokenize(stmt or "") if t.kind == sqlguard.WORD]
    except sqlguard.SqlParseError:
        toks = (stmt or "").upper().split()
    if not toks:
        return False
    if toks[0] in ("INSERT", "UPDATE", "DELETE", "MERGE"):
        return True
    return toks[0] == "SELECT" and "INTO" in toks


def _compat_warning(root, result):
    try:
        from vmc_harness.db import facts
        dev = facts.value(root, "compatibility_level")
    except Exception:
        dev = None
    got = result.get("compatibility_level")
    if dev is not None and got is not None and int(dev) != int(got):
        result["warnings"].append("드라이런 DB 호환성 수준(%s)이 개발 DB(%s)와 다름" % (got, dev))


def _finish_dryrun(root, result, task_id, refused=False):
    store = _dryrun_store(root)
    store.mkdir(parents=True, exist_ok=True)
    write_json(store / ("%s.json" % result["sha256"][:16]), result)
    events.log(root, "db_dryrun", file=result["file"], outcome="refused" if refused else ("error" if result["error"] else "ok"),
               affected_rows=result["affected_rows"], statements=result["statements"],
               rollback_sql=bool(result["rollback_sql"]), task_id=task_id)
    if task_id and not refused:
        result["evidence"] = _make_evidence(root, task_id, result)


def _make_evidence(root, task_id, result):
    data = {k: result[k] for k in ("file", "sha256", "affected_rows", "statements", "batches", "per_batch", "error",
                                   "rolled_back", "rollback_sql", "rollback_note", "compatibility_level", "warnings",
                                   "at") if k in result}
    try:
        from vmc_harness.gates import evidence
        return evidence.make(root, task_id, "db_dryrun", result["file"], data=data)
    except (ImportError, AttributeError):
        pass
    # gates.evidence가 없으면(설치본 일부 누락) 같은 위치·마커 형식으로 직접 남긴다
    d = paths.cache_dir(root) / "evidence" / re.sub(r"[^A-Za-z0-9_.-]", "_", task_id)
    d.mkdir(parents=True, exist_ok=True)
    mid = re.sub(r"[^0-9T]", "", now_iso())[:15] + "-" + result["sha256"][:6]
    write_json(d / (mid + ".json"), data)
    marker = {"marker_id": mid, "task_id": task_id, "created_at": now_iso(), "kind": "db_dryrun",
              "target": result["file"], "file": mid + ".json",
              "sha256": hashlib.sha256((d / (mid + ".json")).read_bytes()).hexdigest(), "by": "vmc-harness"}
    write_json(d / (mid + ".marker.json"), marker)
    return marker


def last_dryrun(root, sql_path):
    """이 파일의 지금 내용(sha)에 대한 드라이런 결과, 없으면 None."""
    p = Path(sql_path)
    if not p.is_absolute():
        p = Path(root) / p
    if not p.is_file():
        return None
    sha = _sha(read_sql_file(p))
    return read_json(_dryrun_store(root) / ("%s.json" % sha[:16]))


# ---------------------------------------------------------------- 운영 적용 승인 기록

def prepare_apply_approval(root, task_id, sql_path):
    """운영 DB 적용 승인 기록(사람 개입 기준 1). 롤백 SQL·드라이런 결과(지금 파일 내용 기준, 오류 없음)가 없으면 거부."""
    from vmc_harness.core import ledger
    p = Path(sql_path)
    if not p.is_absolute():
        p = Path(root) / p
    if not p.is_file():
        raise ledger.MetaGateError("SQL 파일 없음")
    rb = find_rollback_sql(p)
    if not rb:
        raise ledger.MetaGateError("롤백 SQL 없음")
    dr = last_dryrun(root, p)
    if not dr:
        raise ledger.MetaGateError("드라이런 결과 없음(또는 드라이런 뒤 파일이 바뀜)")
    if dr.get("error"):
        raise ledger.MetaGateError("드라이런 실패 상태")
    rel, rb_rel = _rel(root, p), _rel(root, rb)
    lines = len(read_sql_file(p).splitlines())
    verification = ["개발/복제 DB 드라이런: 영향 %d행, 배치 %d개, 오류 없음, 롤백함" % (dr["affected_rows"], dr["statements"])]
    if dr.get("compatibility_level") is not None:
        verification.append("드라이런 DB 호환성 수준 %s" % dr["compatibility_level"])
    verification.extend(dr.get("warnings") or [])
    return ledger.create_approval(
        root, task_id, 1, "운영 DB에 SQL 패치 적용: %s" % rel, "적용", False,
        "롤백 SQL 실행: %s" % rb_rel, verification, lines,
        risk="운영 데이터에 남는 변경 — 자동 롤백 대상 아님",
        detail={"sql_path": rel, "rollback_sql": rb_rel, "dryrun_at": dr.get("at"), "sha256": dr.get("sha256"),
                "affected_rows": dr["affected_rows"]})
