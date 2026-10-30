import os
import tempfile
import unittest
from pathlib import Path

from tests.gates_util import GatesCase
from vmc_harness.db import sqlguard
from vmc_harness.db.sqlguard import apply_auto_top, direct_db_reason, is_direct_db_command, is_select_only

ALLOW = [
    "SELECT * FROM t",
    "select top 5 * from dbo.T_ITEM",
    "SELECT a, b FROM t WHERE c = 'x; DELETE FROM t'",
    "SELECT 'DROP TABLE x' AS s",
    "SELECT [delete], [update] FROM [insert]",
    'SELECT "exec" FROM t',
    "SELECT 1 -- ; DROP TABLE t",
    "/* /* nested */ DROP TABLE t */ SELECT 1",
    "WITH a AS (SELECT 1 x) SELECT * FROM a",
    ";WITH a (x) AS (SELECT 1), b AS (SELECT x FROM a) SELECT * FROM b;",
    "SELECT a FROM t UNION ALL SELECT b FROM u",
    "SELECT a FROM t EXCEPT SELECT a FROM u",
    "(SELECT 1) UNION (SELECT 2)",
    "SELECT * FROM t WITH (NOLOCK) ORDER BY 1",
    "SELECT TOP 5 WITH TIES * FROM t ORDER BY a",
    "SELECT * FROM t ORDER BY a OFFSET 0 ROWS FETCH NEXT 5 ROWS ONLY",
    "SELECT * FROM t WHERE x IN (SELECT y FROM u)",
    "SELECT * FROM Factory.dbo.T_CO_User",
    "SELECT 1 FROM sys.all_objects a CROSS JOIN sys.all_objects b",
    "SELECT N'한글''따옴표' AS s",
    "SELECT * FROM t FOR JSON PATH",
    "SELECT a, COUNT(*) FROM t GROUP BY a WITH ROLLUP",
    "SELECT * FROM db..t",
    "SELECT CAST(x AS nvarchar(10)) FROM t;",
]
DENY = [
    ("", "빈"), ("   ;  ", "빈"), ("-- only comment", "빈"),
    ("INSERT INTO t VALUES (1)", "INSERT"), ("UPDATE t SET a = 1", "UPDATE"), ("DELETE FROM t", "DELETE"),
    ("MERGE t USING u ON 1=1 WHEN MATCHED THEN DELETE;", "MERGE"), ("TRUNCATE TABLE t", "TRUNCATE"),
    ("DROP TABLE t", "DDL"), ("CREATE TABLE x (a int)", "DDL"), ("ALTER TABLE t ADD c int", "DDL"),
    ("EXEC sp_who", "EXEC"), ("EXECUTE dbo.UP_X", "EXEC"), ("SELECT * INTO #x FROM t", "INTO"),
    ("SELECT 1; SELECT 2", "여러 문장"), ("SELECT 1; DELETE FROM t", "여러 문장"), ("SELECT 1 SELECT 2", "여러 문장"),
    ("SELECT 1 WITH c AS (SELECT 2) SELECT * FROM c", "여러 문장"),
    ("WITH a AS (SELECT 1 x) INSERT INTO t SELECT * FROM a", "INSERT"),
    ("WITH a AS (SELECT 1 x) DELETE FROM t", "DELETE"),
    ("SELECT * FROM OPENROWSET('SQLNCLI', 'x', 'SELECT 1')", "OPENROWSET"),
    ("SELECT * FROM OPENQUERY(srv, 'SELECT 1')", "OPENQUERY"),
    ("SELECT * FROM OPENDATASOURCE('x','y').db.dbo.t", "OPENDATASOURCE"),
    ("SELECT xp_cmdshell('dir')", "xp_cmdshell"), ("SELECT * FROM srv.db.dbo.t", "연결된 서버"),
    ("SELECT * FROM [srv].[db].[dbo].[t]", "연결된 서버"), ("SELECT * FROM srv..dbo.t", "연결된 서버"),
    ("SELECT NEXT VALUE FOR dbo.seq", "NEXT VALUE"), ("WAITFOR DELAY '00:01'", "WAITFOR"),
    ("SELECT 1 WAITFOR DELAY '00:00:10'", "WAITFOR"), ("SELECT 'abc", "해석 불가"), ("SELECT /* x", "해석 불가"),
    ("SELECT [abc FROM t", "해석 불가"), ("SELECT (1", "괄호"), ("SELECT 1)", "괄호"),
    ("DECLARE @x int SELECT @x", "변수"), ("SET ROWCOUNT 0 SELECT 1", "SET"), ("USE master SELECT 1", "USE"),
    ("SELECT 1\nGO\nDELETE FROM t", "GO"), ("BEGIN TRAN SELECT 1", "트랜잭션"), ("IF 1=1 SELECT 1", "제어문"),
    ("GRANT SELECT ON t TO u", "권한"), ("BULK INSERT t FROM 'x'", "BULK"), ("DBCC CHECKDB", "관리"),
    ("VALUES (1)", "SELECT 문만"), ("TABLE t", "SELECT 문만"), ("sp_who", "프로시저"),
]


class SelectOnlyTest(unittest.TestCase):
    def test_allow(self):
        for sql in ALLOW:
            ok, reason = is_select_only(sql)
            self.assertTrue(ok, "%r → %s" % (sql, reason))

    def test_deny(self):
        for sql, frag in DENY:
            ok, reason = is_select_only(sql)
            self.assertFalse(ok, sql)
            self.assertIn(frag, reason, "%r → %s" % (sql, reason))

    def test_auto_top(self):
        self.assertEqual(apply_auto_top("SELECT * FROM t", 1000), ("SELECT TOP (1000) * FROM t", "top"))
        self.assertEqual(apply_auto_top("SELECT DISTINCT a FROM t", 7)[0], "SELECT DISTINCT TOP (7) a FROM t")
        self.assertEqual(apply_auto_top("select top 5 * from t", 1000)[1], "kept")
        self.assertEqual(apply_auto_top("SELECT * FROM t ORDER BY a OFFSET 5 ROWS", 9)[1], "kept")
        self.assertEqual(apply_auto_top("SELECT a FROM t UNION SELECT b FROM u", 9)[1], "rowcount")
        self.assertEqual(apply_auto_top("(SELECT 1) UNION (SELECT 2)", 9)[1], "rowcount")
        sql, how = apply_auto_top(";WITH c AS (SELECT TOP 3 1 a FROM t) SELECT * FROM c", 9)
        self.assertEqual((sql, how), (";WITH c AS (SELECT TOP 3 1 a FROM t) SELECT TOP (9) * FROM c", "top"))
        # 서브쿼리 안 TOP은 바깥과 무관
        self.assertEqual(apply_auto_top("SELECT * FROM (SELECT TOP 1 a FROM t) x", 9)[1], "top")
        # TOP을 넣은 결과도 SELECT 판정 통과
        self.assertTrue(is_select_only(apply_auto_top("SELECT * FROM t -- c", 9)[0])[0])

    def test_masked_sql(self):
        m = sqlguard.masked_sql("SELECT * FROM u WHERE name = N'홍길동' AND pwd = 'x' -- password=abc")
        self.assertNotIn("홍길동", m)
        self.assertNotIn("abc", m)
        self.assertIn("'?'", m)


class BatchTest(unittest.TestCase):
    def test_go_split(self):
        text = "USE [Factory];\nGO\nSELECT 1\n/* 주석 안\nGO\n끝 */\nSELECT 2\ngo\nSELECT 'a\nGO\nb'\nGO 3\n\nGO\n-- 주석만\nGO"
        b = sqlguard.split_batches(text)
        self.assertEqual(len(b), 3)
        self.assertEqual(b[0][0].strip(), "USE [Factory];")
        self.assertIn("SELECT 2", b[1][0])
        self.assertEqual(b[2][2], 3)
        self.assertIn("GO\nb'", b[2][0])
        body, removed = sqlguard.strip_use_lines(b[0][0] + "\nSELECT 1")
        self.assertEqual(removed, ["USE [Factory];"])
        self.assertNotIn("USE", body)

    def test_dryrun_blockers(self):
        self.assertEqual(sqlguard.dryrun_blockers("UPDATE t SET a=1; DELETE FROM u"), [])
        self.assertEqual(sqlguard.dryrun_blockers("SET XACT_ABORT ON; BEGIN TRAN; UPDATE t SET a=1; COMMIT"), [])
        self.assertEqual(sqlguard.dryrun_blockers("CREATE PROCEDURE p AS SELECT 1"), [])
        for sql, frag in [("ALTER DATABASE x SET COMPATIBILITY_LEVEL = 150", "ALTER DATABASE"),
                          ("BACKUP DATABASE x TO DISK='y'", "BACKUP"), ("UPDATE t SET a=1; COMMIT", "COMMIT"),
                          ("EXEC xp_cmdshell 'dir'", "xp_cmdshell"), ("DBCC CHECKIDENT('t', RESEED, 0)", "DBCC"),
                          ("UPDATE srv.db.dbo.t SET a=1", "연결된 서버"), ("SET IMPLICIT_TRANSACTIONS ON", "IMPLICIT"),
                          ("UPDATE t SET a=1\nUSE other", "USE"), ("EXEC sp_configure 'x', 1", "sp_configure"),
                          ("BEGIN DISTRIBUTED TRANSACTION", "분산"), ("UPDATE t SET a = 'x", "해석 불가")]:
            got = sqlguard.dryrun_blockers(sql)
            self.assertTrue(any(frag in g for g in got), "%r → %s" % (sql, got))


BLOCK = [
    'sqlcmd -S localhost -U sa -Q "select 1"',
    'SQLCMD.EXE -S . -E -i x.sql',
    'C:\\Tools\\sqlcmd.exe -S . -Q "x"',
    '"C:\\Program Files\\Microsoft SQL Server\\Client SDK\\ODBC\\170\\Tools\\Binn\\SQLCMD.EXE" -S . -Q x',
    'osql -E -Q "x"', 'isql DSN user pass', 'tsql -S server -U user', 'bcp dbo.T out t.dat -S x',
    'freebcp dbo.T in x -S s', 'bsqldb -S s', 'mssql-cli -S x', 'sqlpackage /Action:Publish',
    'Invoke-Sqlcmd -ServerInstance . -Query "SELECT 1"',
    'powershell -Command "Invoke-Sqlcmd -ServerInstance x -Query \'select 1\'"',
    'pwsh -c "Invoke-DbaQuery -SqlInstance x -Query q"',
    '$c = New-Object System.Data.SqlClient.SqlConnection("Server=x;Database=y;")',
    'python -c "import pyodbc; c=pyodbc.connect(\'DRIVER={ODBC Driver 17 for SQL Server};SERVER=x\')"',
    'python3 -c "import pymssql; pymssql.connect(\'h\', \'u\', \'p\')"',
    "python3 - <<'PY'\nimport pymssql\nc = pymssql.connect('h','u','p','db')\nPY",
    "be/.venv/bin/python <<EOF\nimport pyodbc\npyodbc.connect(cs)\nEOF\necho done",
    'echo "import pymssql; pymssql.connect(server=\'x\')" | python',
    'uv run python -c "from app.db import engine; engine.execute(\'DELETE FROM t\')"',
    'poetry run python -c "from x import SessionLocal; s=SessionLocal(); s.execute(q)"',
    'python -c "import sqlalchemy as sa; e=sa.create_engine(url)"',
    'node -e "const sql=require(\'mssql\'); sql.connect(cfg)"',
    'docker exec -it mssql /opt/mssql-tools18/bin/sqlcmd -S localhost',
    'ssh dev "sqlcmd -Q x"', 'cmd /c "osql -E -Q x"', 'bash -c "sqlcmd -Q x"',
    'sudo -u me sqlcmd -Q x', 'FOO=1 sqlcmd -Q x', 'timeout 10 sqlcmd -Q x',
    'vmc-harness db query "SELECT 1" && sqlcmd -Q "x"',
    'vmc-harness db query "SELECT 1"; isql dsn',
    'vmc-harness db query "$(sqlcmd -Q x)"',
    'curl "Server=myhost;Database=Apps;User Id=sa"',
    'export DB_URL="mssql+pyodbc://u:p@h/db"',
    'some-tool --conn "Driver={ODBC Driver 18 for SQL Server};Server=x"',
    'java -jar x.jar jdbc:sqlserver://host;databaseName=y',
]
ALLOW_CMD = [
    'grep -rn sqlcmd README.md', 'rg "pyodbc.connect" be/', 'git commit -m "remove sqlcmd usage"',
    'git log --grep=Invoke-Sqlcmd', 'vmc-harness db query "SELECT * FROM t -- sqlcmd pyodbc.connect"',
    'vmc-harness db dryrun SQL/Patch/010.sql', 'python -m vmc_harness db query "SELECT 1"',
    'python3 -c "import pyodbc; print(pyodbc.drivers())"', 'pip install pymssql', 'pytest be/tests -q',
    'ls -la; npm run build', 'npm test 2>&1 | tail -5', "cat <<'EOF' > notes.txt\nsqlcmd stuff\nEOF",
    'which sqlcmd', 'echo sqlcmd', 'python -m pytest -k mssql', 'cat SQL/deploy_to_database.py',
    'python scripts/build.py', 'python -c "print(1)"', 'Get-Command sqlcmd', '',
]


class DirectDbTest(unittest.TestCase):
    def test_block(self):
        for c in BLOCK:
            self.assertTrue(is_direct_db_command(c), c)
            self.assertIn("DB 직접 접속 차단됨", direct_db_reason(c))

    def test_allow(self):
        for c in ALLOW_CMD:
            self.assertFalse(is_direct_db_command(c), "%r → %s" % (c, direct_db_reason(c)))

    def test_script_file(self):
        d = Path(tempfile.mkdtemp())
        (d / "SQL").mkdir()
        (d / "SQL" / "deploy_to_database.py").write_text("import pyodbc\nconn = pyodbc.connect(cs, autocommit=True)\n")
        (d / "tools").mkdir()
        (d / "tools" / "ok.py").write_text("print('hi')\n")
        (d / "tools" / "__init__.py").write_text("")
        (d / "tools" / "dbq.py").write_text("from sqlalchemy import create_engine\ne = create_engine('mssql+pyodbc://x')\n")
        self.assertTrue(is_direct_db_command("python SQL/deploy_to_database.py --dry-run", cwd=str(d)))
        self.assertTrue(is_direct_db_command(".venv\\Scripts\\python SQL\\deploy_to_database.py", cwd=str(d)))
        self.assertTrue(is_direct_db_command("python -m tools.dbq", cwd=str(d)))
        self.assertFalse(is_direct_db_command("python tools/ok.py", cwd=str(d)))
        self.assertFalse(is_direct_db_command("python missing.py", cwd=str(d)))

    def test_unparseable_falls_back_to_patterns(self):
        self.assertTrue(is_direct_db_command("echo 'unterminated && sqlcmd -Q x"))
        self.assertFalse(is_direct_db_command("echo 'unterminated && ls"))


class ScriptLanguagesAndTextKindTest(unittest.TestCase):
    """고객 재현(합성): 스크립트 파일에 둔 SQL은 python 밖 언어면 놓침 / 실행하지 않는 문자열로 막히면 풀 길이 없음."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        (self.d / "purge.js").write_text("const sql = require('mssql');\nawait sql.connect(cfg);\n"
                                         "await sql.query`DELETE FROM orders WHERE id = 1`;\n")
        (self.d / "q.ts").write_text("import { ConnectionPool } from 'mssql'\nnew ConnectionPool(c).request().query('DELETE FROM t')\n")
        (self.d / "p.ps1").write_text('Invoke-Sqlcmd -ServerInstance . -Query "DELETE FROM t"\r\n')
        (self.d / "run.sh").write_text('#!/bin/sh\nsqlcmd -S . -Q "DELETE FROM t"\n')
        (self.d / "run.bat").write_bytes(b'@echo off\r\nsqlcmd -S . -Q "DELETE FROM t"\r\n')
        (self.d / "ok.js").write_text("console.log('DELETE FROM is just text here')\n")

    def test_script_languages(self):
        from vmc_harness.db.sqlguard import direct_db_verdict
        for c in ("node purge.js", "node ./purge.js --dry-run", "tsx q.ts", "bun run q.ts", "pwsh -File p.ps1",
                  "powershell -ExecutionPolicy Bypass -File .\\p.ps1", "& .\\p.ps1", "bash run.sh", "./run.sh",
                  "cmd /c run.bat", "call run.bat", "run.bat"):
            v = direct_db_verdict(c, cwd=str(self.d))
            self.assertEqual(v and v[1], "exec", "%s → %s" % (c, v))
        for c in ("node ok.js", "node missing.js", "bash missing.sh"):
            self.assertIsNone(direct_db_verdict(c, cwd=str(self.d)), c)

    def test_text_kind(self):
        from vmc_harness.db.sqlguard import direct_db_verdict
        text = ["echo 'unterminated && sqlcmd -Q \"DELETE FROM t\"",   # 해석 실패 → 원문 패턴
                """curl -X POST -d '{"conn":"Server=db1;Database=app;User Id=sa","sql":"DELETE FROM t"}' http://localhost/x""",
                """jq -n '{command: "sqlcmd -S x -Q DELETE"}' --arg x 'a b'""",
                'some-tool --conn "Driver={ODBC Driver 18 for SQL Server};Server=x"']
        for c in text:
            v = direct_db_verdict(c)
            self.assertEqual(v and v[1], "text", "%r → %s" % (c, v))
        for c in ('sqlcmd -S . -Q "DELETE FROM t"', 'ssh dev "sqlcmd -Q x"', 'docker exec db sqlcmd -Q x',
                  'java -jar x.jar jdbc:sqlserver://host;databaseName=y',
                  "python3 - <<'PY'\nimport pymssql\npymssql.connect('h','u','p').cursor().execute('DELETE FROM t')\nPY"):
            v = direct_db_verdict(c)
            self.assertEqual(v and v[1], "exec", "%r → %s" % (c, v))


class DbTextProceedTest(GatesCase):
    def test_text_block_can_proceed_exec_cannot(self):
        cmd = """curl -d '{"conn":"Server=db1;Database=app;User Id=sa"}' http://localhost/x"""
        self.assertDenied(self.hook("PreToolUse", "Bash", {"command": cmd}), "실행하지 않는 문자열")
        self.hook("UserPromptSubmit", prompt="그냥 진행")
        self.assertAllowed(self.hook("PreToolUse", "Bash", {"command": cmd}))
        self.assertDenied(self.hook("PreToolUse", "Bash", {"command": cmd}))   # 1회만
        sql = 'sqlcmd -Q "DELETE FROM t"'
        self.assertDenied(self.hook("PreToolUse", "Bash", {"command": sql}), "DB 직접 접속 차단됨")
        self.hook("UserPromptSubmit", prompt="그냥 진행")
        self.assertDenied(self.hook("PreToolUse", "Bash", {"command": sql}))


if __name__ == "__main__":
    unittest.main()
