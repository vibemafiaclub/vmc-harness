"""DB 조회·드라이런·등록 — 가짜 커넥션(drivers 어댑터와 같은 인터페이스)으로. 실서버 실측은 test_db_live.py."""

import os
import re
import time

from tests.util import HarnessTestCase
from vmc_harness.core import events, ledger, secrets
from vmc_harness.core.jsonio import write_json
from vmc_harness.db import client, credential, drivers, facts


class FakeConn(object):
    driver = "fake"

    def __init__(self, server_name="devbox", db_name="AppsDev", compat=110, perms=(), profile_ok=True,
                 sleep=0, fail_on=None, trancount_drop_on=None, rows=5):
        self.server_name, self.db_name, self.compat = server_name, db_name, compat
        self.perms, self.profile_ok, self.sleep = set(perms), profile_ok, sleep
        self.fail_on, self.trancount_drop_on, self.rows = fail_on, trancount_drop_on, rows
        self.executed, self.rolled_back, self.closed, self.cancelled = [], 0, False, False
        self.trancount = 0

    def _res(self, columns=None, rows=None, rowcounts=None, profiles=None, truncated=False):
        return {"columns": columns or [], "rows": rows or [], "truncated": truncated,
                "rowcounts": rowcounts or [], "profiles": profiles or []}

    def run(self, sql, max_rows=None):
        self.executed.append(sql)
        s = sql.strip()
        if self.sleep and s.upper().startswith("SELECT") and "@@" not in s and "sys." not in s:
            time.sleep(self.sleep)
        if self.fail_on and self.fail_on in s:
            raise drivers.DbError("Invalid object name 'OPENJSON'.")
        if s == "SELECT @@SERVERNAME":
            return self._res(["c"], [[self.server_name]])
        if s == "SELECT DB_NAME()":
            return self._res(["c"], [[self.db_name]])
        if s == "SELECT @@TRANCOUNT":
            return self._res(["c"], [[self.trancount]])
        if "compatibility_level" in s:
            return self._res(["c"], [[self.compat]])
        if "SERVERPROPERTY" in s:
            return self._res(["c"], [["16.0"]])
        if "DATABASEPROPERTYEX" in s:
            return self._res(["c"], [["Korean_Wansung_CI_AS"]])
        m = re.search(r"IS_SRVROLEMEMBER\('(\w+)'\)|IS_ROLEMEMBER\('(\w+)'\)|'DATABASE', '([A-Z ]+)'", s)
        if m:
            key = next(x for x in m.groups() if x)
            return self._res(["c"], [[1 if key in self.perms else 0]])
        if s == "SET STATISTICS PROFILE ON":
            if not self.profile_ok:
                raise drivers.DbError("SHOWPLAN permission denied")
            return self._res()
        if s.startswith("BEGIN TRANSACTION"):
            self.trancount += 1
            return self._res()
        if s.startswith("SET ") or s.startswith("IF @@TRANCOUNT"):
            return self._res()
        if s.upper().startswith("SELECT") or s.upper().startswith(";WITH") or s.upper().startswith("WITH"):
            n = self.rows if max_rows is None else min(self.rows, max_rows)
            return self._res(["id", "name"], [[i, "r%d" % i] for i in range(n)], truncated=max_rows is not None and self.rows > max_rows)
        # 패치 배치: 문장마다 영향 행 수 = 문장 안 숫자 'n행' 표기(합성)
        if self.trancount_drop_on and self.trancount_drop_on in s:
            self.trancount = 0
        profiles, rowcounts = [], []
        for stmt in [x for x in s.split(";") if x.strip()]:
            m2 = re.search(r"/\*rows=(\d+)\*/", stmt)
            n = int(m2.group(1)) if m2 else 0
            profiles.append((stmt.strip(), n))
            if m2:
                rowcounts.append(n)
        return self._res(profiles=profiles, rowcounts=rowcounts)

    def scalar(self, sql):
        r = self.run(sql, max_rows=1)
        return r["rows"][0][0] if r["rows"] else None

    def rollback(self):
        self.rolled_back += 1
        self.trancount = 0

    def cancel(self):
        self.cancelled = True

    def close(self):
        self.closed = True


class DbTestBase(HarnessTestCase):
    def setUp(self):
        super().setUp()
        os.environ["VMC_HARNESS_SECRETS_BACKEND"] = "file"
        self.conns = []

    def factory(self, **kw):
        def make(info, pw, timeout):
            c = FakeConn(**kw)
            c.info, c.pw, c.timeout = info, pw, timeout
            self.conns.append(c)
            return c
        return make

    def register(self, profile="query", **kw):
        fields = {"server": "devbox", "port": "1433", "database": "AppsDev", "user": "ro_user"}
        fields.update(kw.pop("fields", {}))
        return credential.register(self.root, profile, fields, "s3cret!", True, conn_factory=self.factory(**kw), log=lambda *_: None)


class CredentialTest(DbTestBase):
    def test_register_stores_reference_only(self):
        info = self.register()
        data = client.load_db_json(self.root)
        self.assertEqual(data["query"]["server"], "devbox")
        raw = client.db_json_path(self.root).read_text(encoding="utf-8")
        self.assertNotIn("s3cret!", raw)
        self.assertEqual(secrets.get_secret(info["secret_ref"]), "s3cret!")
        f = facts.load(self.root)
        lvl = [x for x in f["facts"] if x["key"] == "compatibility_level"][0]
        self.assertEqual(lvl["value"], 110)
        self.assertIn("SELECT compatibility_level FROM sys.databases WHERE name = DB_NAME()", lvl["command"])
        self.assertIn("OPENJSON", facts.value(self.root, "unavailable_features"))

    def test_refuse_production_names(self):
        for fields in ({"database": "DIPortal_PROD"}, {"server": "prd-sql01"}, {"database": "운영DB"},
                       {"server": "sql-live.corp"}, {"database": "PortalProduction"}):
            with self.assertRaises(credential.CredentialRefused):
                self.register(fields=fields)
        self.assertEqual(client.load_db_json(self.root), {})

    def test_refuse_when_connected_db_looks_production(self):
        with self.assertRaises(credential.CredentialRefused):
            self.register(server_name="SQLPROD01")
        self.assertEqual(client.load_db_json(self.root), {})

    def test_refuse_without_dev_confirmation(self):
        with self.assertRaises(credential.CredentialRefused):
            credential.register(self.root, "query", {"server": "s", "database": "d", "user": "u"}, "p", False,
                                conn_factory=self.factory(), log=lambda *_: None)

    def test_refuse_explicit_prod_names_from_config(self):
        write_json(self.root / ".harness" / "config.json", {"project_id": self.project_id, "profile": "standard",
                                                            "db": {"prod_names": ["Factory"]}})
        with self.assertRaises(credential.CredentialRefused):
            self.register(fields={"database": "Factory"})

    def test_query_profile_must_be_read_only(self):
        with self.assertRaises(credential.CredentialRefused) as cm:
            self.register(perms=("db_datawriter", "INSERT"))
        self.assertIn("읽기 전용", str(cm.exception))
        self.register("dryrun", perms=("db_owner",))  # 드라이런 계정은 쓰기 권한 가능(항상 롤백)
        self.assertIn("dryrun", client.load_db_json(self.root))

    def test_name_tokens_not_overmatching(self):
        self.assertIsNone(client.looks_production(self.root, "DeliveryDev", "AppsDev", "ProductCatalog_Dev", "Factory", "Apps"))
        for n in ("AppsProd01", "SQLPROD01", "prdsql", "DIPortal-PRD", "운영_복제"):
            self.assertIsNotNone(client.looks_production(self.root, n), n)

    def test_interactive_refuses_non_tty(self):
        import sys
        import io
        old = sys.stdin
        sys.stdin = io.StringIO("devbox\n")
        try:
            with self.assertRaises(credential.CredentialRefused):
                credential.interactive(self.root, "query")
            with self.assertRaises(RuntimeError):
                secrets.read_secret_from_terminal("pw: ")
        finally:
            sys.stdin = old

    def test_remove_profile_deletes_secret(self):
        info = self.register()
        client.remove_profile(self.root, "query")
        self.assertIsNone(secrets.get_secret(info["secret_ref"]))
        self.assertNotIn("query", client.load_db_json(self.root))


class QueryTest(DbTestBase):
    def test_query_truncates_and_rolls_back(self):
        self.register()
        res = client.query(self.root, "SELECT * FROM t", conn_factory=self.factory(rows=500))
        self.assertTrue(res["truncated"])
        self.assertEqual(len(res["rows"]), 200)
        self.assertEqual(res["auto_top"], "top")
        c = self.conns[-1]
        self.assertTrue(any("TOP (1000)" in s for s in c.executed))
        self.assertTrue(any("LOCK_TIMEOUT" in s for s in c.executed))
        self.assertIn("BEGIN TRANSACTION", c.executed)
        self.assertGreaterEqual(c.rolled_back, 1)
        self.assertTrue(c.closed)
        self.assertEqual(c.pw, "s3cret!")

    def test_union_uses_rowcount(self):
        self.register()
        res = client.query(self.root, "SELECT a FROM t UNION SELECT b FROM u", conn_factory=self.factory())
        self.assertEqual(res["auto_top"], "rowcount")
        self.assertTrue(any("SET ROWCOUNT 1000" in s for s in self.conns[-1].executed))

    def test_refused_never_connects(self):
        self.register()
        n = len(self.conns)
        with self.assertRaises(client.DbRefused):
            client.query(self.root, "DELETE FROM t", conn_factory=self.factory())
        self.assertEqual(len(self.conns), n)
        ev = events.read(self.root, "db_query")[-1]
        self.assertEqual(ev["outcome"], "refused")

    def test_events_mask_literals(self):
        self.register()
        client.query(self.root, "SELECT * FROM u WHERE name = N'홍길동'", conn_factory=self.factory())
        ev = events.read(self.root, "db_query")[-1]
        self.assertNotIn("홍길동", ev["sql"])
        self.assertEqual(ev["outcome"], "ok")

    def test_not_registered(self):
        with self.assertRaises(client.NotRegistered):
            client.query(self.root, "SELECT 1", conn_factory=self.factory())

    def test_client_side_timeout_cancels(self):
        import vmc_harness.core.config as cfg
        self.register()
        values, meta = cfg.defaults()
        old = (values["db.query_timeout_s"], values["db.cancel_grace_s"])
        values["db.query_timeout_s"], values["db.cancel_grace_s"] = 1, 0
        try:
            t0 = time.time()
            with self.assertRaises(drivers.DbTimeout):
                client.query(self.root, "SELECT * FROM slow", conn_factory=self.factory(sleep=3))
            self.assertLess(time.time() - t0, 2.5)
            self.assertTrue(self.conns[-1].cancelled)
            self.assertEqual(events.read(self.root, "db_query")[-1]["outcome"], "timeout")
        finally:
            values["db.query_timeout_s"], values["db.cancel_grace_s"] = old


class DryrunTest(DbTestBase):
    PATCH = ("USE [Factory];\nGO\nSET XACT_ABORT ON;\nGO\n"
             "UPDATE t SET a = 1 /*rows=10*/;\nDELETE FROM t WHERE b = 2 /*rows=3*/;\nSELECT 1 /*rows=1*/;\nGO\n"
             "INSERT t VALUES (1) /*rows=2*/\nGO 2\n")

    def setUp(self):
        super().setUp()
        self.register()
        self.register("dryrun", perms=("db_owner",))
        self.write("SQL/Patch/010_PATCH_x.sql", self.PATCH)

    def test_counts_and_always_rolls_back(self):
        res = client.dryrun(self.root, "SQL/Patch/010_PATCH_x.sql", task_id="T-261022-aaaaaa", conn_factory=self.factory())
        self.assertIsNone(res["error"])
        self.assertEqual(res["affected_rows"], 10 + 3 + 2 + 2)
        self.assertEqual(res["statements"], 4)
        self.assertEqual(res["ignored_use"], ["USE [Factory];"])
        self.assertTrue(res["rolled_back"])
        self.assertEqual(res["rollback_note"], "롤백 SQL 없음")
        self.assertEqual(res["count_method"], "statistics_profile")
        c = self.conns[-1]
        self.assertGreaterEqual(c.rolled_back, 1)
        self.assertTrue(any(s.startswith("IF @@TRANCOUNT > 0 ROLLBACK") for s in c.executed))
        self.assertFalse(any("USE [Factory]" in s for s in c.executed))
        self.assertEqual(c.info["user"], "ro_user")  # 같은 fields로 등록했으므로
        ev = (self.root / ".harness" / "cache" / "evidence" / "T-261022-aaaaaa")
        markers = list(ev.glob("*.marker.json"))
        self.assertEqual(len(markers), 1)
        import json
        m = json.loads(markers[0].read_text(encoding="utf-8"))
        self.assertEqual((m["kind"], m["target"]), ("db_dryrun", "SQL/Patch/010_PATCH_x.sql"))

    def test_rowcount_fallback_without_showplan(self):
        res = client.dryrun(self.root, "SQL/Patch/010_PATCH_x.sql", conn_factory=self.factory(profile_ok=False))
        self.assertEqual(res["count_method"], "rowcount")
        self.assertEqual(res["affected_rows"], 10 + 3 + 1 + 2 + 2)

    def test_error_is_reported_and_rolled_back(self):
        self.write("SQL/Patch/020_PATCH_json.sql", "SELECT * FROM OPENJSON(@j);\nUPDATE t SET a=1 /*rows=1*/;")
        res = client.dryrun(self.root, "SQL/Patch/020_PATCH_json.sql", conn_factory=self.factory(fail_on="OPENJSON"))
        self.assertIn("OPENJSON", res["error"])
        self.assertTrue(res["rolled_back"])
        self.assertGreaterEqual(self.conns[-1].rolled_back, 1)

    def test_patch_ending_transaction_stops(self):
        self.write("SQL/Patch/030_PATCH_rb.sql", "UPDATE t SET a=1 /*rows=1*/;\nIF @@ERROR <> 0 ROLLBACK\nGO\nUPDATE u SET b=2 /*rows=5*/;")
        res = client.dryrun(self.root, "SQL/Patch/030_PATCH_rb.sql",
                            conn_factory=self.factory(trancount_drop_on="ROLLBACK"))
        self.assertIn("트랜잭션을 끝냄", res["error"])
        self.assertFalse(any("UPDATE u" in s for s in self.conns[-1].executed))

    def test_non_transactional_refused_before_connect(self):
        self.write("SQL/Patch/040.sql", "ALTER DATABASE CURRENT SET COMPATIBILITY_LEVEL = 150")
        n = len(self.conns)
        with self.assertRaises(client.DbRefused):
            client.dryrun(self.root, "SQL/Patch/040.sql", conn_factory=self.factory())
        self.assertEqual(len(self.conns), n)
        self.write("SQL/Patch/041.sql", "UPDATE t SET a=1;\nCOMMIT;")
        with self.assertRaises(client.DbRefused):
            client.dryrun(self.root, "SQL/Patch/041.sql", conn_factory=self.factory())

    def test_refuses_production_target(self):
        with self.assertRaises(client.DbRefused):
            client.dryrun(self.root, "SQL/Patch/010_PATCH_x.sql", conn_factory=self.factory(db_name="App_PRD"))

    def test_compat_warning(self):
        res = client.dryrun(self.root, "SQL/Patch/010_PATCH_x.sql", conn_factory=self.factory(compat=150))
        self.assertTrue(any("호환성" in w for w in res["warnings"]))

    def test_utf16_and_cp949_files(self):
        p = self.root / "SQL" / "Patch" / "050.sql"
        p.write_bytes("UPDATE t SET n = N'한글' /*rows=4*/;".encode("utf-16"))
        self.assertEqual(client.dryrun(self.root, p, conn_factory=self.factory())["affected_rows"], 4)
        p.write_bytes("UPDATE t SET n = N'한글' /*rows=6*/;".encode("cp949"))
        self.assertEqual(client.dryrun(self.root, p, conn_factory=self.factory())["affected_rows"], 6)

    def test_rollback_sql_conventions(self):
        base = "SQL/Patch/060_PATCH_y.sql"
        self.write(base, "UPDATE t SET a=1;")
        self.assertIsNone(client.find_rollback_sql(self.root / base))
        for rb in ("SQL/Patch/060_PATCH_y_rollback.sql", "SQL/Patch/060_PATCH_y.rollback.sql",
                   "SQL/Patch/rollback/060_PATCH_y.sql", "SQL/Rollback/060_PATCH_y.sql", "SQL/Patch/060_ROLLBACK_y.sql"):
            p = self.write(rb, "UPDATE t SET a=0;")
            got = client.find_rollback_sql(self.root / base)
            self.assertTrue(got is not None and os.path.samefile(str(got), str(p)), rb)
            p.unlink()


class ApprovalTest(DbTestBase):
    def setUp(self):
        super().setUp()
        self.register()
        self.write("SQL/Patch/010_PATCH_x.sql", "UPDATE t SET a = 1 /*rows=3*/;")

    def test_requires_rollback_sql(self):
        client.dryrun(self.root, "SQL/Patch/010_PATCH_x.sql", conn_factory=self.factory())
        with self.assertRaises(ledger.MetaGateError) as cm:
            client.prepare_apply_approval(self.root, "T-1", "SQL/Patch/010_PATCH_x.sql")
        self.assertIn("롤백 SQL 없음", str(cm.exception))

    def test_requires_fresh_successful_dryrun(self):
        self.write("SQL/Patch/010_PATCH_x_rollback.sql", "UPDATE t SET a = 0;")
        with self.assertRaises(ledger.MetaGateError):
            client.prepare_apply_approval(self.root, "T-1", "SQL/Patch/010_PATCH_x.sql")
        client.dryrun(self.root, "SQL/Patch/010_PATCH_x.sql", conn_factory=self.factory())
        rec = client.prepare_apply_approval(self.root, "T-1", "SQL/Patch/010_PATCH_x.sql")
        self.assertEqual(rec["criterion"], 1)
        self.assertFalse(rec["reversible"])
        self.assertIn("영향 3행", rec["verification"][0])
        self.assertIn("010_PATCH_x_rollback.sql", rec["revert_how"])
        # 드라이런 뒤 파일이 바뀌면 다시 거부
        self.write("SQL/Patch/010_PATCH_x.sql", "UPDATE t SET a = 2 /*rows=3*/;")
        with self.assertRaises(ledger.MetaGateError):
            client.prepare_apply_approval(self.root, "T-1", "SQL/Patch/010_PATCH_x.sql")

    def test_failed_dryrun_refused(self):
        self.write("SQL/Patch/010_PATCH_x_rollback.sql", "UPDATE t SET a = 0;")
        client.dryrun(self.root, "SQL/Patch/010_PATCH_x.sql", conn_factory=self.factory(fail_on="UPDATE"))
        with self.assertRaises(ledger.MetaGateError):
            client.prepare_apply_approval(self.root, "T-1", "SQL/Patch/010_PATCH_x.sql")


class PatchDirTest(DbTestBase):
    def test_inactive_without_db(self):
        self.assertIsNone(client.patch_dir(self.root))

    def test_config_override_and_default(self):
        self.register()
        self.assertEqual(client.patch_dir(self.root), "SQL")
        write_json(self.root / ".harness" / "config.json", {"project_id": self.project_id, "profile": "standard",
                                                            "db": {"patch_dir": "db\\patches\\"}})
        self.assertEqual(client.patch_dir(self.root), "db/patches")
