"""실서버 실측 — 버리는 MSSQL 컨테이너에서만. VMC_HARNESS_MSSQL_TEST="host,port,sa비밀번호" + pymssql 설치 시에만 돈다.

예) docker run -d --platform linux/amd64 -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD=... -p 14333:1433 mcr.microsoft.com/mssql/server:2022-latest
    VMC_HARNESS_MSSQL_TEST="127.0.0.1,14333,..." python -m unittest tests.test_db_live
시험용 DB(VmhSmokeDb, 호환성 110)·로그인(vmh_smoke_ro/vmh_smoke_dev)을 만든다 — 운영·공용 서버에 돌리지 않는다.
"""

import os
import time
import unittest

from tests.util import HarnessTestCase

SPEC = os.environ.get("VMC_HARNESS_MSSQL_TEST")
try:
    import pymssql  # noqa: F401
    HAVE = True
except ImportError:
    HAVE = False

RO_PW, DEV_PW = "RoLive!2026x", "DevLive!2026x"


@unittest.skipUnless(SPEC and HAVE, "실서버 시험 환경 없음")
class LiveMssqlTest(HarnessTestCase):
    @classmethod
    def setUpClass(cls):
        import pymssql
        host, port, sa = SPEC.split(",", 2)
        cls.host, cls.port = host, port
        c = pymssql.connect(server=host, port=port, user="sa", password=sa, autocommit=True)
        cur = c.cursor()
        for s in ["IF DB_ID('VmhSmokeDb') IS NULL CREATE DATABASE VmhSmokeDb",
                  "ALTER DATABASE VmhSmokeDb SET COMPATIBILITY_LEVEL = 110",
                  "IF SUSER_ID('vmh_smoke_ro') IS NULL CREATE LOGIN vmh_smoke_ro WITH PASSWORD='%s', CHECK_POLICY=OFF" % RO_PW,
                  "IF SUSER_ID('vmh_smoke_dev') IS NULL CREATE LOGIN vmh_smoke_dev WITH PASSWORD='%s', CHECK_POLICY=OFF" % DEV_PW]:
            cur.execute(s)
        cur.execute("USE VmhSmokeDb")
        for s in ["IF USER_ID('vmh_smoke_ro') IS NULL CREATE USER vmh_smoke_ro FOR LOGIN vmh_smoke_ro",
                  "ALTER ROLE db_datareader ADD MEMBER vmh_smoke_ro",
                  "IF USER_ID('vmh_smoke_dev') IS NULL CREATE USER vmh_smoke_dev FOR LOGIN vmh_smoke_dev",
                  "ALTER ROLE db_owner ADD MEMBER vmh_smoke_dev",
                  "IF OBJECT_ID('dbo.T_ITEM') IS NOT NULL DROP TABLE dbo.T_ITEM",
                  "CREATE TABLE dbo.T_ITEM (id int identity primary key, name nvarchar(50), qty int)",
                  "INSERT dbo.T_ITEM(name, qty) SELECT TOP 1500 N'품목', 1 FROM sys.all_objects a CROSS JOIN sys.all_objects b"]:
            cur.execute(s)
        c.close()

    def setUp(self):
        super().setUp()
        os.environ["VMC_HARNESS_SECRETS_BACKEND"] = "file"
        from vmc_harness.db import credential
        base = {"server": self.host, "port": self.port, "database": "VmhSmokeDb"}
        with self.assertRaises(credential.CredentialRefused):  # 쓰기 가능 계정은 조회용으로 못 쓴다
            credential.register(self.root, "query", dict(base, user="vmh_smoke_dev"), DEV_PW, True, log=lambda *_: None)
        credential.register(self.root, "query", dict(base, user="vmh_smoke_ro"), RO_PW, True, log=lambda *_: None)
        credential.register(self.root, "dryrun", dict(base, user="vmh_smoke_dev"), DEV_PW, True, log=lambda *_: None)

    def test_query_dryrun_facts(self):
        from vmc_harness.db import client, facts
        self.assertEqual(facts.value(self.root, "compatibility_level"), 110)
        r = client.query(self.root, "SELECT id, name FROM dbo.T_ITEM ORDER BY id")
        self.assertEqual((len(r["rows"]), r["truncated"], r["auto_top"]), (200, True, "top"))
        self.write("SQL/Patch/010_PATCH_qty.sql",
                   "USE [Factory];\nGO\nUPDATE dbo.T_ITEM SET qty = qty + 1 WHERE id <= 10;\n"
                   "IF 1 = 1 DELETE FROM dbo.T_ITEM WHERE id BETWEEN 11 AND 13;\nGO\n"
                   "INSERT dbo.T_ITEM(name, qty) VALUES (N'a', 1), (N'b', 2);\nGO 2\n")
        d = client.dryrun(self.root, "SQL/Patch/010_PATCH_qty.sql", task_id="T-261022-live00")
        self.assertIsNone(d["error"])
        self.assertEqual((d["affected_rows"], d["count_method"]), (17, "statistics_profile"))
        after = client.query(self.root, "SELECT COUNT(*), SUM(qty) FROM dbo.T_ITEM")["rows"][0]
        self.assertEqual(after, [1500, 1500])  # 롤백됨
        self.write("SQL/Patch/020_PATCH_json.sql", "SELECT * FROM OPENJSON(N'[1]');\nUPDATE dbo.T_ITEM SET qty = 0;")
        d2 = client.dryrun(self.root, "SQL/Patch/020_PATCH_json.sql")
        self.assertIn("OPENJSON", d2["error"])  # 호환성 110에서 깨지는 SQL을 드라이런이 잡는다

    def test_timeout_stops_server_query(self):
        from vmc_harness.db import client, drivers
        import vmc_harness.core.config as cfg
        values, _ = cfg.defaults()
        old = values["db.query_timeout_s"]
        values["db.query_timeout_s"] = 3
        try:
            t0 = time.time()
            with self.assertRaises(drivers.DbTimeout):
                client.query(self.root, "SELECT COUNT_BIG(*) FROM sys.all_objects a CROSS JOIN sys.all_objects b "
                                        "CROSS JOIN sys.all_objects c CROSS JOIN sys.all_objects d")
            self.assertLess(time.time() - t0, 3 + cfg.get("db.cancel_grace_s") + 2)
        finally:
            values["db.query_timeout_s"] = old
