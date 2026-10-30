"""MSSQL 드라이버 어댑터 (선택 의존성 pymssql → pyodbc). 엔진의 다른 곳은 이 인터페이스만 본다.

    conn = connect(info, password, query_timeout_s)   # DbConn
    conn.run(sql, max_rows=None) -> {"columns", "rows", "truncated", "rowcounts", "profiles"}
        한 배치를 실행하고 결과 집합(첫 번째)·결과 없는 문장들의 영향 행 수·STATISTICS PROFILE(문장, 실제 행 수)을 돌려준다
        (pymssql은 결과 없는 문장의 rowcount를 따로 주지 않는다 — 실측. 드라이런은 STATISTICS PROFILE로 센다)
    conn.scalar(sql) -> 첫 행 첫 값
    conn.rollback() / conn.cancel() / conn.close()
    conn.driver -> "pymssql" | "pyodbc"

autocommit은 끈다 — 모든 실행은 트랜잭션 안이고 하네스는 commit을 부르지 않는다.
테스트는 같은 인터페이스의 가짜 커넥션을 쓴다(tests/test_db_client.py).
"""

import datetime as _dt
import decimal
import sys
import uuid

from vmc_harness.core import config, paths


class DriverMissing(RuntimeError):
    pass


class DbError(RuntimeError):
    """DB가 돌려준 오류(메시지는 마스킹해서 쓴다)."""


class DbTimeout(RuntimeError):
    pass


ODBC_PREFERENCE = ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server",
                   "ODBC Driver 13 for SQL Server", "SQL Server Native Client 11.0", "SQL Server", "FreeTDS")


def pydeps_dir():
    """드라이버 설치 위치 — 하네스 홈 아래(시스템 파이썬을 건드리지 않는다, `remove`가 같이 지운다)."""
    return paths.home() / "pydeps" / ("py%d%d" % sys.version_info[:2])


def _add_pydeps():
    d = str(pydeps_dir())
    if d not in sys.path:
        sys.path.append(d)


def available():
    """설치돼 쓸 수 있는 드라이버 이름 목록(우선순위 순)."""
    _add_pydeps()
    out = []
    try:
        import pymssql  # noqa: F401
        out.append("pymssql")
    except Exception:
        pass
    try:
        import pyodbc
        if _odbc_driver_name(pyodbc):
            out.append("pyodbc")
    except Exception:
        pass
    return out


def _odbc_driver_name(pyodbc):
    try:
        names = list(pyodbc.drivers())
    except Exception:
        return None
    for pref in ODBC_PREFERENCE:
        if pref in names:
            return pref
    return None


def try_install(log=None, root=None):
    """드라이버가 없으면 설치를 시도한다(spec 02 — 안내가 아니라 시도). (ok, 마지막 출력 한 줄)"""
    from vmc_harness.core.proc import run
    if available():
        return True, ""
    target = pydeps_dir()
    target.mkdir(parents=True, exist_ok=True)
    timeout = config.get("db.install_timeout_s", root)
    pkgs = ["pymssql"] if sys.platform != "win32" else ["pymssql", "pyodbc"]
    last = ""
    for pkg in pkgs:
        if log:
            log("드라이버 설치 시도: %s" % pkg)
        rc, out, err = run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                            "--target", str(target), pkg], timeout=timeout)
        last = ((err or out).strip().splitlines() or [""])[-1]
        _purge_import_cache()
        if available():
            return True, ""
    return False, last


def _purge_import_cache():
    import importlib
    importlib.invalidate_caches()


def pick(preferred=None):
    names = available()
    if preferred and preferred in names:
        return preferred
    return names[0] if names else None


def connect(info, password, query_timeout_s, driver=None, root=None):
    """info: db.json 프로파일(server, port, database, user, driver, trust_server_certificate)."""
    name = pick(driver or info.get("driver"))
    if not name:
        raise DriverMissing("MSSQL 드라이버 없음(pymssql·pyodbc)")
    login_timeout = config.get("db.connect_timeout_s", root)
    if name == "pymssql":
        import pymssql
        kw = dict(server=info["server"], user=info["user"], password=password, database=info["database"],
                  login_timeout=login_timeout, timeout=query_timeout_s, autocommit=False, appname="vmc-harness")
        if info.get("port"):
            kw["port"] = str(info["port"])
        try:
            raw = pymssql.connect(**kw)
        except Exception as e:
            raise DbError(_err_text(e))
        return PymssqlConn(raw)
    import pyodbc
    drv = info.get("odbc_driver") or _odbc_driver_name(pyodbc)
    server = info["server"] + ("," + str(info["port"]) if info.get("port") else "")

    def v(x):
        return "{" + str(x).replace("}", "}}") + "}"

    parts = ["DRIVER=" + v(drv), "SERVER=" + v(server), "DATABASE=" + v(info["database"]),
             "UID=" + v(info["user"]), "PWD=" + v(password), "APP=vmc-harness"]
    if info.get("trust_server_certificate", True):
        parts.append("TrustServerCertificate=yes")
    try:
        raw = pyodbc.connect(";".join(parts), autocommit=False, timeout=login_timeout)
        raw.timeout = int(query_timeout_s)  # SQL_ATTR_QUERY_TIMEOUT — 넘으면 드라이버가 서버에 취소(attention)를 보낸다
    except Exception as e:
        raise DbError(_err_text(e))
    return PyodbcConn(raw)


def _err_text(e):
    from vmc_harness.core.mask import mask
    args = getattr(e, "args", None) or ()
    parts = []
    for a in args:
        if isinstance(a, bytes):
            a = a.decode("utf-8", "replace")
        parts.append(str(a))
    text = " ".join(parts) or str(e)
    text = text.split("DB-Lib error message")[0]  # pymssql 꼬리(FreeTDS 일반 문구) 제거
    return mask(" ".join(text.split()))


def jsonable(v):
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "0x" + bytes(v).hex()
    return str(v)


class _Base(object):
    driver = ""

    def __init__(self, raw):
        self.raw = raw
        self.cur = None

    def scalar(self, sql):
        res = self.run(sql, max_rows=1)
        return res["rows"][0][0] if res["rows"] else None

    def rollback(self):
        try:
            self.raw.rollback()
        except Exception:
            pass

    def close(self):
        try:
            self.raw.close()
        except Exception:
            pass

    def _collect(self, cur, max_rows):
        """결과 집합 중 첫 번째(일반 결과)를 rows로, SET STATISTICS PROFILE 집합은 profiles[(문장, 실제 행 수)]로,
        결과 없는 문장의 rowcount는 rowcounts로 모은다."""
        columns, rows, truncated, rowcounts, profiles = None, [], False, [], []
        while True:
            if cur.description:
                cols = [d[0] for d in cur.description]
                if "StmtText" in cols and "Rows" in cols and "Parent" in cols:
                    prof = cur.fetchall()
                    if prof:
                        top = prof[0]
                        profiles.append((top[cols.index("StmtText")] or "", top[cols.index("Rows")] or 0))
                elif columns is None:
                    columns = cols
                    want = (max_rows + 1) if max_rows is not None else None
                    fetched = cur.fetchmany(want) if want is not None else cur.fetchall()
                    if max_rows is not None and len(fetched) > max_rows:
                        truncated = True
                        fetched = fetched[:max_rows]
                    rows = [[jsonable(x) for x in r] for r in fetched]
                else:
                    cur.fetchall()
            else:
                rc = getattr(cur, "rowcount", -1)
                if rc is not None and rc >= 0:
                    rowcounts.append(rc)
            try:
                more = cur.nextset()
            except Exception as e:
                raise DbError(_err_text(e))
            if not more:
                break
        return {"columns": columns or [], "rows": rows, "truncated": truncated, "rowcounts": rowcounts,
                "profiles": profiles}


class PymssqlConn(_Base):
    driver = "pymssql"

    def run(self, sql, max_rows=None):
        cur = self.raw.cursor()
        self.cur = cur
        try:
            cur.execute(sql)
            return self._collect(cur, max_rows)
        except DbError:
            raise
        except Exception as e:
            raise DbError(_err_text(e))

    def cancel(self):
        try:
            self.raw._conn.cancel()
        except Exception:
            self.close()


class PyodbcConn(_Base):
    driver = "pyodbc"

    def run(self, sql, max_rows=None):
        cur = self.raw.cursor()
        self.cur = cur
        try:
            cur.execute(sql)
            return self._collect(cur, max_rows)
        except DbError:
            raise
        except Exception as e:
            msg = _err_text(e)
            if "HYT00" in msg or "timeout" in msg.lower():
                raise DbTimeout(msg)
            raise DbError(msg)

    def cancel(self):
        try:
            if self.cur is not None:
                self.cur.cancel()
                return
        except Exception:
            pass
        self.close()
