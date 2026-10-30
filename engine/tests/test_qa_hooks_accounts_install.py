"""QA: 훅(범위 밖 쓰기·시나리오 출처·범위 넓히기) · 계정(키체인·마스킹) · 서브에이전트 설치."""

import json
from unittest import mock

from tests.util import HarnessTestCase
from vmc_harness.core.jsonio import write_json
from vmc_harness.hooks.contract import HookContext


def ctx(root, tool, write_paths=(), tool_input=None, command=None, agent="claude"):
    return HookContext(event="PreToolUse", agent=agent, raw={}, root=str(root), tool_name=tool,
                       tool_input=tool_input or {}, command=command, write_paths=list(write_paths))


class HooksTest(HarnessTestCase):
    def test_scenario_write_needs_source(self):
        from vmc_harness.loop import handlers
        bad = {"id": "x", "expectations": [{"text": "보인다"}]}
        r = handlers.on_pre_tool(ctx(self.root, "Write", [".harness/qa/scenarios/x.json"], {"content": json.dumps(bad)}))
        self.assertTrue(r and r.block)
        self.assertIn("출처 없음", r.reason)
        self.assertTrue(r.inject and r.inject[0].startswith("[vmc-harness:gate_violation]"))
        good = {"id": "x", "expectations": [{"text": "보인다", "source": {"type": "current_behavior", "ref": "a.py::f"}}]}
        r = handlers.on_pre_tool(ctx(self.root, "Write", [".harness/qa/scenarios/x.json"], {"content": json.dumps(good)}))
        self.assertIsNone(r)

    def test_policy_widening_blocked(self):
        from vmc_harness.loop import handlers
        write_json(self.root / ".harness/qa/data-policy.json", {"db_access": "browser_only", "allowed": []})
        new = {"db_access": "browser_only", "allowed": [{"entity": "users", "ops": ["delete"]}]}
        r = handlers.on_pre_tool(ctx(self.root, "Write", [".harness/qa/data-policy.json"], {"content": json.dumps(new)}))
        self.assertTrue(r and r.block)
        self.assertIn("사람 확인", r.reason)

    def test_active_qa_blocks_out_of_scope(self):
        from vmc_harness.loop import handlers
        from vmc_harness.qa import runner
        c = ctx(self.root, "Bash", command="sqlcmd -S dev -d DI -Q \"update t set a=1\"")
        self.assertIsNone(handlers.on_pre_tool(c))  # QA 중이 아니면 qa 쪽은 관여 안 함(DB 차단은 gates 몫)
        runner._set_active(self.root, "login")
        r = handlers.on_pre_tool(c)
        self.assertTrue(r and r.block)
        self.assertIn("DB 직접 접근", r.reason)
        write_json(self.root / ".harness/qa/data-policy.json",
                   {"forbidden_urls": ["*/admin/*/delete*"]})
        r = handlers.on_pre_tool(ctx(self.root, "mcp__playwright__browser_navigate",
                                     tool_input={"url": "http://localhost:3000/admin/u/delete"}))
        self.assertTrue(r and r.block)

    def test_nested_does_nothing(self):
        import os
        from vmc_harness.loop import handlers
        os.environ["VMC_HARNESS_NESTED"] = "1"
        bad = {"id": "x", "expectations": [{"text": "보인다"}]}
        self.assertIsNone(handlers.on_pre_tool(ctx(self.root, "Write", [".harness/qa/scenarios/x.json"],
                                                   {"content": json.dumps(bad)})))


class AccountsTest(HarnessTestCase):
    def test_add_list_mask_copy(self):
        from vmc_harness.qa import accounts
        store = {}
        with mock.patch("vmc_harness.core.secrets.set_secret", side_effect=lambda n, v: store.__setitem__(n, v)), \
                mock.patch("vmc_harness.core.secrets.get_secret", side_effect=lambda n: store.get(n)), \
                mock.patch("vmc_harness.core.secrets.delete_secret", side_effect=lambda n: store.pop(n, None)):
            accounts.add(self.root, "qa-user", "qauser01", "S3cret!pw", role="기안자")
            p = accounts.accounts_path(self.root)
            self.assertTrue(str(p).startswith(str(self.home)))  # 프로젝트 밖
            text = p.read_text(encoding="utf-8")
            self.assertNotIn("S3cret", text)  # 파일엔 비밀번호 없음
            self.assertEqual(store["qa:%s:qa-user" % self.project_id], "S3cret!pw")
            lst = accounts.listing(self.root)
            self.assertEqual(lst[0]["username"][:2], "qa")
            self.assertNotIn("qauser01", json.dumps(lst))
            with mock.patch("vmc_harness.qa.accounts.run", return_value=(0, "", "")) as r:
                ok, _ = accounts.copy_to_clipboard(self.root, "qa-user")
                self.assertTrue(ok)
                self.assertEqual(r.call_args.kwargs.get("input_text"), "S3cret!pw")
            self.assertTrue(accounts.remove(self.root, "qa-user"))
            self.assertEqual(store, {})
        self.assertFalse(any(self.root.rglob("qa-accounts.json")))


class InstallTest(HarnessTestCase):
    def test_register_unregister(self):
        from vmc_harness.qa import install
        self.write(".codex/config.toml", 'model = "gpt-5"\n')
        r = install.register(self.root)
        self.assertEqual(r, {"claude": "installed", "codex": "installed"})
        md = (self.root / ".claude/agents/vmc-qa.md").read_text(encoding="utf-8")
        self.assertTrue(md.startswith("---\nname: vmc-qa\n"))
        self.assertNotIn("\ntools:", md)  # 세션 도구(고객 브라우저 도구)를 그대로
        self.assertIn("스크린샷", md)
        cfg = (self.root / ".codex/config.toml").read_text(encoding="utf-8")
        self.assertIn('model = "gpt-5"', cfg)
        self.assertIn("[agents.vmc-qa]", cfg)
        try:
            import tomllib
        except ImportError:
            tomllib = None
        if tomllib:
            role = tomllib.loads((self.root / ".codex/agents/vmc-qa.toml").read_text(encoding="utf-8"))
            self.assertTrue(role["developer_instructions"].strip())
            self.assertEqual(tomllib.loads(cfg)["agents"]["vmc-qa"]["config_file"], "agents/vmc-qa.toml")
        install.register(self.root)  # 두 번 해도 블록 하나
        self.assertEqual((self.root / ".codex/config.toml").read_text(encoding="utf-8").count("[agents.vmc-qa]"), 1)
        removed = install.unregister(self.root)
        self.assertIn(".claude/agents/vmc-qa.md", removed)
        self.assertFalse((self.root / ".claude/agents/vmc-qa.md").exists())
        self.assertEqual((self.root / ".codex/config.toml").read_text(encoding="utf-8").strip(), 'model = "gpt-5"')

    def test_customer_file_untouched(self):
        from vmc_harness.qa import install
        self.write(".claude/agents/vmc-qa.md", "고객 파일")
        self.assertEqual(install.register(self.root, agents=("claude",))["claude"], "skipped")
        install.unregister(self.root)
        self.assertEqual((self.root / ".claude/agents/vmc-qa.md").read_text(encoding="utf-8"), "고객 파일")
