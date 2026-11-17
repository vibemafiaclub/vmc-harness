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
            self.assertNotIn("vmc-harness", r.reason.split("] ", 1)[1])  # 해결 방법 지시 없음(P6)

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

    def test_secret_reason_tagged_and_human_only(self):
        """사유는 허용 목록 문구(꼬리표)이고 차단 기록이 남는다 — 이름 없는 고정 문구로 바뀌지 않는다(현장 결함 C8)."""
        from vmc_harness.gates import state
        from vmc_harness.hooks import dispatcher
        c = self.ctx(tool="Bash", command="security find-generic-password -s x -w")
        c.session_id = "s1"
        r = handlers.on_pre_tool(c)
        self.assertTrue(r.block)
        self.assertTrue(dispatcher.valid_message(r.reason, False)[0], r.reason)
        self.assertIn("비밀값", r.reason)
        self.assertEqual(state.last_block(str(self.root), "s1")["gate"], "secret")
        self.assertEqual(c.state.get("recorded_block_gate"), "secret")

    def test_secret_pattern_narrow(self):
        """`export`와 `keychain`이 한 줄의 다른 명령에 따로 있으면 비밀값 접근이 아니다(현장 결함 C8 — 읽기 명령 오탐)."""
        for cmd in ('ls src/x/ | head; grep -n "interface\\|export type" a.ts | head; grep -rn "keychain" src',
                    "export FOO=1; ls keychain-notes/"):
            self.assertIsNone(handlers.on_pre_tool(self.ctx(tool="Bash", command=cmd)), cmd)
        for cmd in ("security export -k login.keychain -o out.p12", "security dump-keychain"):
            r = handlers.on_pre_tool(self.ctx(tool="Bash", command=cmd))
            self.assertTrue(r and r.block, cmd)

    def test_session_start_never_raises(self):
        ctx = HookContext(event="SessionStart", agent="claude", raw={}, root=str(self.root))
        self.assertIsNone(handlers.on_session_start(ctx))
