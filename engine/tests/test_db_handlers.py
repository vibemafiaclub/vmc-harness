import os

from tests.util import HarnessTestCase
from vmc_harness.core.jsonio import write_json
from vmc_harness.db import handlers
from vmc_harness.hooks.contract import HookContext


class DbHandlersTest(HarnessTestCase):
    def setUp(self):
        super().setUp()
        write_json(self.root / ".harness" / "config.json", {"project_id": self.project_id, "profile": "standard",
                                                            "db": {"patch_dir": "SQL"}})

    def ctx(self, tool="Write", paths=(), command=None, tool_input=None):
        return HookContext(event="PreToolUse", agent="claude", raw={}, root=str(self.root), tool_name=tool,
                           write_paths=list(paths), command=command, tool_input=tool_input or {})

    def test_sql_inside_patch_dir_ok(self):
        for p in ("SQL/Patch/010_x.sql", "sql/Seed/1.SQL", "SQL/SP/dbo.UP_X.StoredProcedure.sql",
                  str(self.root / "SQL" / "Patch" / "a.sql"), "be/app/main.py"):
            self.assertIsNone(handlers.on_pre_tool(self.ctx(paths=[p])), p)

    def test_sql_outside_blocked(self):
        for p in ("be/tests/fixtures/x.sql", "patch.sql", "SQLX/a.sql", "../other/SQL/a.sql", "/tmp/a.sql"):
            r = handlers.on_pre_tool(self.ctx(paths=[p]))
            self.assertTrue(r and r.block, p)
            self.assertIn("SQL 파일 쓰기 위치 위반", r.reason)
            self.assertNotIn("vmc-harness", r.reason)  # 해결 방법 지시 없음(P6)

    def test_shell_write_targets(self):
        for cmd in ('echo "UPDATE t" > be/fix.sql', "cat a | tee out.sql", "cp SQL/a.sql be/b.sql",
                    "Set-Content -Path be\\x.sql -Value q", "sed -i '' 's/a/b/' be/x.sql"):
            r = handlers.on_pre_tool(self.ctx(tool="Bash", command=cmd))
            self.assertTrue(r and r.block, cmd)
        for cmd in ('echo "x" > SQL/Patch/a.sql', "cat be/x.sql", "npm test 2>&1 | tee log.txt", "cp be/b.sql SQL/Patch/b.sql"):
            self.assertIsNone(handlers.on_pre_tool(self.ctx(tool="Bash", command=cmd)), cmd)

    def test_inactive_for_non_db_project(self):
        write_json(self.root / ".harness" / "config.json", {"project_id": self.project_id, "profile": "standard"})
        self.assertIsNone(handlers.on_pre_tool(self.ctx(paths=["supabase/migrations/1.sql"])))

    def test_secret_access_blocked(self):
        home = os.environ["VMC_HARNESS_HOME"]
        for cmd in ("security find-generic-password -s vmc-harness -a db:x:query -w",
                    "cat ~/.vmc-harness/secrets/db%3Ax", "type C:\\Users\\a\\.vmc-harness\\secrets\\x",
                    "cat ~/.vmc-harness/projects/abc/db.json"):
            r = handlers.on_pre_tool(self.ctx(tool="Bash", command=cmd))
            self.assertTrue(r and r.block, cmd)
        r = handlers.on_pre_tool(self.ctx(tool="Read", tool_input={"file_path": home + "/.vmc-harness/projects/p/db.json"}))
        self.assertTrue(r and r.block)
        self.assertIsNone(handlers.on_pre_tool(self.ctx(tool="Bash", command="vmc-harness db-credential --show")))
        self.assertIsNone(handlers.on_pre_tool(self.ctx(tool="Read", tool_input={"file_path": "be/app/db.json"})))

    def test_session_start_never_raises(self):
        ctx = HookContext(event="SessionStart", agent="claude", raw={}, root=str(self.root))
        self.assertIsNone(handlers.on_session_start(ctx))
